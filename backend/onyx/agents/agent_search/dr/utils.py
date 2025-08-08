import re

from langchain.schema.messages import BaseMessage
from langchain.schema.messages import HumanMessage

from onyx.agents.agent_search.dr.enums import DRPath
from onyx.agents.agent_search.dr.models import AggregatedDRContext
from onyx.agents.agent_search.dr.models import IterationAnswer
from onyx.agents.agent_search.dr.models import OrchestrationClarificationInfo
from onyx.agents.agent_search.kb_search.graph_utils import build_document_context
from onyx.context.search.models import InferenceSection

CITATION_PREFIX = "CITE:"


def extract_document_citations(
    answer: str, claims: list[str]
) -> tuple[list[int], str, list[str]]:
    """
    Finds all citations of the form [1], [2, 3], etc. and returns the list of cited indices,
    as well as the answer and claims with the citations replaced with [<CITATION_PREFIX>1],
    etc., to help with citation deduplication later on.
    """
    citations: set[int] = set()

    # Pattern to match both single citations [1] and multiple citations [1, 2, 3]
    # This regex matches:
    # - \[(\d+)\] for single citations like [1]
    # - \[(\d+(?:,\s*\d+)*)\] for multiple citations like [1, 2, 3]
    pattern = re.compile(r"\[(\d+(?:,\s*\d+)*)\]")

    def _extract_and_replace(match: re.Match[str]) -> str:
        numbers = [int(num) for num in match.group(1).split(",")]
        citations.update(numbers)
        return "".join(f"[{CITATION_PREFIX}{num}]" for num in numbers)

    new_answer = pattern.sub(_extract_and_replace, answer)
    new_claims = [pattern.sub(_extract_and_replace, claim) for claim in claims]

    return list(citations), new_answer, new_claims


def aggregate_context(
    iteration_responses: list[IterationAnswer],
    include_documents: bool = False,
    include_answers_claims: bool = True,
) -> AggregatedDRContext:
    """
    Converts the iteration response into a single string with unified citations.
    For example,
        it 1: the answer is x [3][4]. {3: doc_abc, 4: doc_xyz}
        it 2: blah blah [1, 3]. {1: doc_xyz, 3: doc_pqr}
    Output:
        it 1: the answer is x [1][2].
        it 2: blah blah [2][3]
        [1]: doc_xyz
        [2]: doc_abc
        [3]: doc_pqr
    """
    # mapping of document id to citation number
    global_citations: dict[str, int] = {}
    global_documents: list[InferenceSection] = []
    output_strings: list[str] = []

    # build output string
    for iteration_response in sorted(
        iteration_responses,
        key=lambda x: (x.iteration_nr, x.parallelization_nr),
    ):
        output_strings.append(
            f"Iteration: {iteration_response.iteration_nr}, "
            f"Question {iteration_response.parallelization_nr}"
        )
        output_strings.append(f"Tool: {iteration_response.tool.value}")
        output_strings.append(f"Question: {iteration_response.question}")

        answer_str = iteration_response.answer
        claims_str = (
            "".join(f"\n  - {claim}" for claim in iteration_response.claims or [])
            or "No claims provided"
        )

        # compute global citation and replace citation in string
        local_citations = iteration_response.cited_documents
        local_retrieved_document_ids: list[str] = []
        for local_number, cited_doc in local_citations.items():
            cited_doc_id = cited_doc.center_chunk.document_id
            if cited_doc_id not in global_citations:
                global_documents.append(cited_doc)
                global_citations[cited_doc_id] = len(global_documents)
            global_number = global_citations[cited_doc_id]

            answer_str = answer_str.replace(
                f"[{CITATION_PREFIX}{local_number}]", f"[{global_number}]"
            )
            claims_str = claims_str.replace(
                f"[{CITATION_PREFIX}{local_number}]", f"[{global_number}]"
            )
            local_retrieved_document_ids.append(f"[{global_number}]")

        if include_answers_claims:
            output_strings.append(f"Answer: {answer_str}")
            output_strings.append(f"Claims: {claims_str}")
        else:
            local_retrieved_document_ids_str = ", ".join(local_retrieved_document_ids)
            output_strings.append(
                f"Retrieved documents: {local_retrieved_document_ids_str}"
            )

        output_strings.append("\n---\n")

    # add document contents if requested
    if include_documents:
        # note: will probably need to merge sections properly with dedup_inference_section_list
        # at the very start, and use those to build the global citation numbers,
        # if we intend of frequently calling this with include_documents=True
        output_strings.append("Cited document contents:")
        for doc in global_documents:
            output_strings.append(
                build_document_context(
                    doc, global_citations[doc.center_chunk.document_id]
                )
            )
            output_strings.append("\n---\n")

    return AggregatedDRContext(
        context="\n".join(output_strings),
        cited_documents=global_documents,
    )


def get_chat_history_string(chat_history: list[BaseMessage], max_messages: int) -> str:
    """
    Get the chat history (up to max_messages) as a string.
    """
    # get past max_messages USER, ASSISTANT message pairs
    past_messages = chat_history[-max_messages * 2 :]
    return (
        "...\n"
        if len(chat_history) > len(past_messages)
        else ""
        "\n".join(
            ("user" if isinstance(msg, HumanMessage) else "you")
            + f": {str(msg.content).strip()}"
            for msg in past_messages
        )
    )


def get_prompt_question(
    question: str, clarification: OrchestrationClarificationInfo | None
) -> str:
    if clarification:
        clarification_question = clarification.clarification_question
        clarification_response = clarification.clarification_response
        return (
            f"Initial User Question: {question}\n"
            f"(Clarification Question: {clarification_question}\n"
            f"User Response: {clarification_response})"
        )

    return question


def create_tool_call_string(query_path: DRPath, query_list: list[str]) -> str:
    """
    Create a string representation of the tool call.
    """
    questions_str = "\n  - ".join(query_list)
    return f"Tool: {query_path.value}\n\nQuestions:\n{questions_str}"
