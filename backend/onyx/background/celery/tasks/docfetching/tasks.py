import multiprocessing
import os
import time
import traceback
from datetime import datetime
from datetime import timezone
from http import HTTPStatus
from time import sleep

import sentry_sdk
from celery import Celery
from celery import shared_task
from celery import Task
from redis.lock import Lock as RedisLock

from onyx.background.celery.apps.app_base import task_logger
from onyx.background.celery.celery_utils import httpx_init_vespa_pool
from onyx.background.celery.memory_monitoring import emit_process_memory
from onyx.background.celery.tasks.indexing.tasks import ConnectorIndexingLogBuilder
from onyx.background.celery.tasks.indexing.utils import IndexingCallback
from onyx.background.celery.tasks.models import ConnectorIndexingContext
from onyx.background.celery.tasks.models import IndexingWatchdogTerminalStatus
from onyx.background.celery.tasks.models import SimpleJobResult
from onyx.background.indexing.job_client import SimpleJob
from onyx.background.indexing.job_client import SimpleJobClient
from onyx.background.indexing.job_client import SimpleJobException
from onyx.background.indexing.run_indexing import run_indexing_entrypoint
from onyx.configs.app_configs import MANAGED_VESPA
from onyx.configs.app_configs import VESPA_CLOUD_CERT_PATH
from onyx.configs.app_configs import VESPA_CLOUD_KEY_PATH
from onyx.configs.constants import CELERY_INDEXING_LOCK_TIMEOUT
from onyx.configs.constants import CELERY_INDEXING_WATCHDOG_CONNECTOR_TIMEOUT
from onyx.configs.constants import CELERY_TASK_WAIT_FOR_FENCE_TIMEOUT
from onyx.configs.constants import OnyxCeleryTask
from onyx.connectors.exceptions import ConnectorValidationError
from onyx.db.connector_credential_pair import get_connector_credential_pair_from_id
from onyx.db.engine.sql_engine import get_session_with_current_tenant
from onyx.db.index_attempt import get_index_attempt
from onyx.db.index_attempt import mark_attempt_canceled
from onyx.db.index_attempt import mark_attempt_failed
from onyx.redis.redis_connector import RedisConnector
from onyx.redis.redis_connector_index import RedisConnectorIndex
from onyx.redis.redis_pool import get_redis_client
from onyx.utils.logger import setup_logger
from onyx.utils.variable_functionality import global_version
from shared_configs.configs import SENTRY_DSN

logger = setup_logger()


def docfetching_task(
    app: Celery,
    index_attempt_id: int,
    cc_pair_id: int,
    search_settings_id: int,
    is_ee: bool,
    tenant_id: str,
) -> None:
    """
    TODO: update docstring to reflect docfetching
    Indexing task. For a cc pair, this task pulls all document IDs from the source
    and compares those IDs to locally stored documents and deletes all locally stored IDs missing
    from the most recently pulled document ID list

    acks_late must be set to False. Otherwise, celery's visibility timeout will
    cause any task that runs longer than the timeout to be redispatched by the broker.
    There appears to be no good workaround for this, so we need to handle redispatching
    manually.

    Returns None if the task did not run (possibly due to a conflict).
    Otherwise, returns an int >= 0 representing the number of indexed docs.

    NOTE: if an exception is raised out of this task, the primary worker will detect
    that the task transitioned to a "READY" state but the generator_complete_key doesn't exist.
    This will cause the primary worker to abort the indexing attempt and clean up.
    """

    # Since connector_indexing_proxy_task spawns a new process using this function as
    # the entrypoint, we init Sentry here.
    if SENTRY_DSN:
        sentry_sdk.init(
            dsn=SENTRY_DSN,
            traces_sample_rate=0.1,
        )
        logger.info("Sentry initialized")
    else:
        logger.debug("Sentry DSN not provided, skipping Sentry initialization")

    logger.info(
        f"Indexing spawned task starting: "
        f"attempt={index_attempt_id} "
        f"tenant={tenant_id} "
        f"cc_pair={cc_pair_id} "
        f"search_settings={search_settings_id}"
    )

    # 20 is the documented default for httpx max_keepalive_connections
    if MANAGED_VESPA:
        httpx_init_vespa_pool(
            20, ssl_cert=VESPA_CLOUD_CERT_PATH, ssl_key=VESPA_CLOUD_KEY_PATH
        )
    else:
        httpx_init_vespa_pool(20)

    redis_connector = RedisConnector(tenant_id, cc_pair_id)
    redis_connector_index = redis_connector.new_index(search_settings_id)

    r = get_redis_client()

    if redis_connector.delete.fenced:
        raise SimpleJobException(
            f"Indexing will not start because connector deletion is in progress: "
            f"attempt={index_attempt_id} "
            f"cc_pair={cc_pair_id} "
            f"fence={redis_connector.delete.fence_key}",
            code=IndexingWatchdogTerminalStatus.BLOCKED_BY_DELETION.code,
        )

    if redis_connector.stop.fenced:
        raise SimpleJobException(
            f"Indexing will not start because a connector stop signal was detected: "
            f"attempt={index_attempt_id} "
            f"cc_pair={cc_pair_id} "
            f"fence={redis_connector.stop.fence_key}",
            code=IndexingWatchdogTerminalStatus.BLOCKED_BY_STOP_SIGNAL.code,
        )

    # this wait is needed to avoid a race condition where
    # the primary worker sends the task and it is immediately executed
    # before the primary worker can finalize the fence
    start = time.monotonic()
    while True:
        if time.monotonic() - start > CELERY_TASK_WAIT_FOR_FENCE_TIMEOUT:
            raise SimpleJobException(
                f"connector_indexing_task - timed out waiting for fence to be ready: "
                f"fence={redis_connector.permissions.fence_key}",
                code=IndexingWatchdogTerminalStatus.FENCE_READINESS_TIMEOUT.code,
            )

        if not redis_connector_index.fenced:  # The fence must exist
            raise SimpleJobException(
                f"connector_indexing_task - fence not found: fence={redis_connector_index.fence_key}",
                code=IndexingWatchdogTerminalStatus.FENCE_NOT_FOUND.code,
            )

        payload = redis_connector_index.payload  # The payload must exist
        if not payload:
            raise SimpleJobException(
                "connector_indexing_task: payload invalid or not found",
                code=IndexingWatchdogTerminalStatus.FENCE_NOT_FOUND.code,
            )

        if payload.index_attempt_id is None or payload.celery_task_id is None:
            logger.info(
                f"connector_indexing_task - Waiting for fence: fence={redis_connector_index.fence_key}"
            )
            sleep(1)
            continue

        if payload.index_attempt_id != index_attempt_id:
            raise SimpleJobException(
                f"connector_indexing_task - id mismatch. Task may be left over from previous run.: "
                f"task_index_attempt={index_attempt_id} "
                f"payload_index_attempt={payload.index_attempt_id}",
                code=IndexingWatchdogTerminalStatus.FENCE_MISMATCH.code,
            )

        logger.info(
            f"connector_indexing_task - Fence found, continuing...: fence={redis_connector_index.fence_key}"
        )
        break

    # set thread_local=False since we don't control what thread the indexing/pruning
    # might run our callback with
    lock: RedisLock = r.lock(
        redis_connector_index.generator_lock_key,
        timeout=CELERY_INDEXING_LOCK_TIMEOUT,
        thread_local=False,
    )

    acquired = lock.acquire(blocking=False)
    if not acquired:
        logger.warning(
            f"Indexing task already running, exiting...: "
            f"index_attempt={index_attempt_id} "
            f"cc_pair={cc_pair_id} "
            f"search_settings={search_settings_id}"
        )

        raise SimpleJobException(
            f"Indexing task already running, exiting...: "
            f"index_attempt={index_attempt_id} "
            f"cc_pair={cc_pair_id} "
            f"search_settings={search_settings_id}",
            code=IndexingWatchdogTerminalStatus.TASK_ALREADY_RUNNING.code,
        )

    payload.started = datetime.now(timezone.utc)
    redis_connector_index.set_fence(payload)

    try:
        with get_session_with_current_tenant() as db_session:
            attempt = get_index_attempt(db_session, index_attempt_id)
            if not attempt:
                raise SimpleJobException(
                    f"Index attempt not found: index_attempt={index_attempt_id}",
                    code=IndexingWatchdogTerminalStatus.INDEX_ATTEMPT_MISMATCH.code,
                )

            cc_pair = get_connector_credential_pair_from_id(
                db_session=db_session,
                cc_pair_id=cc_pair_id,
            )

            if not cc_pair:
                raise SimpleJobException(
                    f"cc_pair not found: cc_pair={cc_pair_id}",
                    code=IndexingWatchdogTerminalStatus.INDEX_ATTEMPT_MISMATCH.code,
                )

            if not cc_pair.connector:
                raise SimpleJobException(
                    f"Connector not found: cc_pair={cc_pair_id} connector={cc_pair.connector_id}",
                    code=IndexingWatchdogTerminalStatus.INDEX_ATTEMPT_MISMATCH.code,
                )

            if not cc_pair.credential:
                raise SimpleJobException(
                    f"Credential not found: cc_pair={cc_pair_id} credential={cc_pair.credential_id}",
                    code=IndexingWatchdogTerminalStatus.INDEX_ATTEMPT_MISMATCH.code,
                )

        # define a callback class
        callback = IndexingCallback(
            os.getppid(),
            redis_connector,
            lock,
            r,
            redis_connector_index,
        )

        logger.info(
            f"Indexing spawned task running entrypoint: attempt={index_attempt_id} "
            f"tenant={tenant_id} "
            f"cc_pair={cc_pair_id} "
            f"search_settings={search_settings_id}"
        )

        # This is where the heavy/real work happens
        run_indexing_entrypoint(
            app,
            index_attempt_id,
            tenant_id,
            cc_pair_id,
            is_ee,
            callback=callback,
        )

        # get back the total number of indexed docs and return it
        redis_connector_index.get_progress()
        redis_connector_index.set_generator_complete(HTTPStatus.OK.value)
    except ConnectorValidationError:
        raise SimpleJobException(
            f"Indexing task failed: attempt={index_attempt_id} "
            f"tenant={tenant_id} "
            f"cc_pair={cc_pair_id} "
            f"search_settings={search_settings_id}",
            code=IndexingWatchdogTerminalStatus.CONNECTOR_VALIDATION_ERROR.code,
        )

    except Exception as e:
        logger.exception(
            f"Indexing spawned task failed: attempt={index_attempt_id} "
            f"tenant={tenant_id} "
            f"cc_pair={cc_pair_id} "
            f"search_settings={search_settings_id}"
        )

        # special bulletproofing ... truncate long exception messages
        # for exception types that require more args, this will fail
        # thus the try/except
        try:
            sanitized_e = type(e)(str(e)[:1024])
            sanitized_e.__traceback__ = e.__traceback__
            raise sanitized_e
        except Exception:
            raise e

    finally:
        if lock.owned():
            lock.release()

    logger.info(
        f"Indexing spawned task finished: attempt={index_attempt_id} "
        f"cc_pair={cc_pair_id} "
        f"search_settings={search_settings_id}"
    )
    os._exit(0)  # ensure process exits cleanly


def process_job_result(
    job: SimpleJob,
    connector_source: str | None,
    redis_connector_index: RedisConnectorIndex,
    log_builder: ConnectorIndexingLogBuilder,
) -> SimpleJobResult:
    result = SimpleJobResult()
    result.connector_source = connector_source

    if job.process:
        result.exit_code = job.process.exitcode

    if job.status != "error":
        result.status = IndexingWatchdogTerminalStatus.SUCCEEDED
        return result

    ignore_exitcode = False

    # In EKS, there is an edge case where successful tasks return exit
    # code 1 in the cloud due to the set_spawn_method not sticking.
    # We've since worked around this, but the following is a safe way to
    # work around this issue. Basically, we ignore the job error state
    # if the completion signal is OK.
    status_int = redis_connector_index.get_completion()
    if status_int:
        status_enum = HTTPStatus(status_int)
        if status_enum == HTTPStatus.OK:
            ignore_exitcode = True

    if ignore_exitcode:
        result.status = IndexingWatchdogTerminalStatus.SUCCEEDED
        task_logger.warning(
            log_builder.build(
                "Indexing watchdog - spawned task has non-zero exit code "
                "but completion signal is OK. Continuing...",
                exit_code=str(result.exit_code),
            )
        )
    else:
        if result.exit_code is not None:
            result.status = IndexingWatchdogTerminalStatus.from_code(result.exit_code)

        result.exception_str = job.exception()

    return result


@shared_task(
    name=OnyxCeleryTask.CONNECTOR_DOC_FETCHING_TASK,
    bind=True,
    acks_late=False,
    track_started=True,
)
def connector_indexing_proxy_task(
    self: Task,
    index_attempt_id: int,
    cc_pair_id: int,
    search_settings_id: int,
    tenant_id: str,
) -> None:
    """celery out of process task execution strategy is pool=prefork, but it uses fork,
    and forking is inherently unstable.

    To work around this, we use pool=threads and proxy our work to a spawned task.

    TODO(rkuo): refactor this so that there is a single return path where we canonically
    log the result of running this function.

    NOTE: we try/except all db access in this function because as a watchdog, this function
    needs to be extremely stable.
    """
    start = time.monotonic()

    result = SimpleJobResult()

    ctx = ConnectorIndexingContext(
        tenant_id=tenant_id,
        cc_pair_id=cc_pair_id,
        search_settings_id=search_settings_id,
        index_attempt_id=index_attempt_id,
    )

    log_builder = ConnectorIndexingLogBuilder(ctx)

    task_logger.info(
        log_builder.build(
            "Indexing watchdog - starting",
            mp_start_method=str(multiprocessing.get_start_method()),
        )
    )

    if not self.request.id:
        task_logger.error("self.request.id is None!")

    client = SimpleJobClient()
    task_logger.info(f"submitting connector_indexing_task with tenant_id={tenant_id}")

    job = client.submit(
        docfetching_task,
        self.app,
        index_attempt_id,
        cc_pair_id,
        search_settings_id,
        global_version.is_ee_version(),
        tenant_id,
    )

    if not job or not job.process:
        result.status = IndexingWatchdogTerminalStatus.SPAWN_FAILED
        task_logger.info(
            log_builder.build(
                "Indexing watchdog - finished",
                status=str(result.status.value),
                exit_code=str(result.exit_code),
            )
        )
        return

    # Ensure the process has moved out of the starting state
    num_waits = 0
    while True:
        if num_waits > 15:
            result.status = IndexingWatchdogTerminalStatus.SPAWN_NOT_ALIVE
            task_logger.info(
                log_builder.build(
                    "Indexing watchdog - finished",
                    status=str(result.status.value),
                    exit_code=str(result.exit_code),
                )
            )
            job.release()
            return

        if job.process.is_alive() or job.process.exitcode is not None:
            break

        sleep(1)
        num_waits += 1

    task_logger.info(
        log_builder.build(
            "Indexing watchdog - spawn succeeded",
            pid=str(job.process.pid),
        )
    )

    redis_connector = RedisConnector(tenant_id, cc_pair_id)
    redis_connector_index = redis_connector.new_index(search_settings_id)

    # Track the last time memory info was emitted
    last_memory_emit_time = 0.0

    # track the last ttl and the time it was observed
    last_activity_ttl_observed: float = time.monotonic()
    last_activity_ttl: int = 0

    try:
        with get_session_with_current_tenant() as db_session:
            index_attempt = get_index_attempt(
                db_session=db_session,
                index_attempt_id=index_attempt_id,
                eager_load_cc_pair=True,
            )
            if not index_attempt:
                raise RuntimeError("Index attempt not found")

            result.connector_source = (
                index_attempt.connector_credential_pair.connector.source.value
            )

        redis_connector_index.set_active()  # renew active signal

        # prime the connector active signal (renewed inside the connector)
        redis_connector_index.set_connector_active()

        while True:
            sleep(5)

            now = time.monotonic()

            # renew watchdog signal (this has a shorter timeout than set_active)
            redis_connector_index.set_watchdog(True)

            # renew active signal
            redis_connector_index.set_active()

            # if the job is done, clean up and break
            if job.done():
                try:
                    result = process_job_result(
                        job, result.connector_source, redis_connector_index, log_builder
                    )
                except Exception:
                    task_logger.exception(
                        log_builder.build(
                            "Indexing watchdog - spawned task exceptioned"
                        )
                    )
                finally:
                    job.release()
                    break

            # log the memory usage for tracking down memory leaks / connector-specific memory issues
            pid = job.process.pid
            if pid is not None:
                # Only emit memory info once per minute (60 seconds)
                current_time = time.monotonic()
                if current_time - last_memory_emit_time >= 60.0:
                    emit_process_memory(
                        pid,
                        "indexing_worker",
                        {
                            "cc_pair_id": cc_pair_id,
                            "search_settings_id": search_settings_id,
                            "index_attempt_id": index_attempt_id,
                        },
                    )
                    last_memory_emit_time = current_time

            # if a termination signal is detected, break (exit point will clean up)
            if self.request.id and redis_connector_index.terminating(self.request.id):
                task_logger.warning(
                    log_builder.build("Indexing watchdog - termination signal detected")
                )

                result.status = IndexingWatchdogTerminalStatus.TERMINATED_BY_SIGNAL
                break

            # if activity timeout is detected, break (exit point will clean up)
            ttl = redis_connector_index.connector_active_ttl()
            if ttl < 0:
                # verify expectations around ttl
                last_observed = last_activity_ttl_observed - now
                if now > last_activity_ttl_observed + last_activity_ttl:
                    task_logger.warning(
                        log_builder.build(
                            "Indexing watchdog - activity timeout exceeded",
                            last_observed=f"{last_observed:.2f}s",
                            last_ttl=f"{last_activity_ttl}",
                            timeout=f"{CELERY_INDEXING_WATCHDOG_CONNECTOR_TIMEOUT}s",
                        )
                    )

                    result.status = (
                        IndexingWatchdogTerminalStatus.TERMINATED_BY_ACTIVITY_TIMEOUT
                    )
                    break
                else:
                    task_logger.warning(
                        log_builder.build(
                            "Indexing watchdog - activity timeout expired unexpectedly, "
                            "waiting for last observed TTL before exiting",
                            last_observed=f"{last_observed:.2f}s",
                            last_ttl=f"{last_activity_ttl}",
                            timeout=f"{CELERY_INDEXING_WATCHDOG_CONNECTOR_TIMEOUT}s",
                        )
                    )
            else:
                last_activity_ttl_observed = now
                last_activity_ttl = ttl

            # if the spawned task is still running, restart the check once again
            # if the index attempt is not in a finished status
            try:
                with get_session_with_current_tenant() as db_session:
                    index_attempt = get_index_attempt(
                        db_session=db_session, index_attempt_id=index_attempt_id
                    )

                    if not index_attempt:
                        continue

                    if not index_attempt.is_finished():
                        continue

            except Exception:
                task_logger.exception(
                    log_builder.build(
                        "Indexing watchdog - transient exception looking up index attempt"
                    )
                )
                continue

    except Exception as e:
        result.status = IndexingWatchdogTerminalStatus.WATCHDOG_EXCEPTIONED
        if isinstance(e, ConnectorValidationError):
            # No need to expose full stack trace for validation errors
            result.exception_str = str(e)
        else:
            result.exception_str = traceback.format_exc()

    # handle exit and reporting
    elapsed = time.monotonic() - start
    if result.exception_str is not None:
        # print with exception
        try:
            with get_session_with_current_tenant() as db_session:
                failure_reason = (
                    f"Spawned task exceptioned: exit_code={result.exit_code}"
                )
                mark_attempt_failed(
                    ctx.index_attempt_id,
                    db_session,
                    failure_reason=failure_reason,
                    full_exception_trace=result.exception_str,
                )
        except Exception:
            task_logger.exception(
                log_builder.build(
                    "Indexing watchdog - transient exception marking index attempt as failed"
                )
            )

        normalized_exception_str = "None"
        if result.exception_str:
            normalized_exception_str = result.exception_str.replace(
                "\n", "\\n"
            ).replace('"', '\\"')

        task_logger.warning(
            log_builder.build(
                "Indexing watchdog - finished",
                source=result.connector_source,
                status=result.status.value,
                exit_code=str(result.exit_code),
                exception=f'"{normalized_exception_str}"',
                elapsed=f"{elapsed:.2f}s",
            )
        )

        redis_connector_index.set_watchdog(False)
        raise RuntimeError(f"Exception encountered: traceback={result.exception_str}")

    # print without exception
    if result.status == IndexingWatchdogTerminalStatus.TERMINATED_BY_SIGNAL:
        try:
            with get_session_with_current_tenant() as db_session:
                logger.exception(
                    f"Marking attempt {index_attempt_id} as canceled due to termination signal"
                )
                mark_attempt_canceled(
                    index_attempt_id,
                    db_session,
                    "Connector termination signal detected",
                )
        except Exception:
            task_logger.exception(
                log_builder.build(
                    "Indexing watchdog - transient exception marking index attempt as canceled"
                )
            )

        job.cancel()
    elif result.status == IndexingWatchdogTerminalStatus.TERMINATED_BY_ACTIVITY_TIMEOUT:
        try:
            with get_session_with_current_tenant() as db_session:
                mark_attempt_failed(
                    index_attempt_id,
                    db_session,
                    "Indexing watchdog - activity timeout exceeded: "
                    f"attempt={index_attempt_id} "
                    f"timeout={CELERY_INDEXING_WATCHDOG_CONNECTOR_TIMEOUT}s",
                )
        except Exception:
            logger.exception(
                log_builder.build(
                    "Indexing watchdog - transient exception marking index attempt as failed"
                )
            )
        job.cancel()
    else:
        pass

    task_logger.info(
        log_builder.build(
            "Indexing watchdog - finished",
            source=result.connector_source,
            status=str(result.status.value),
            exit_code=str(result.exit_code),
            elapsed=f"{elapsed:.2f}s",
        )
    )

    redis_connector_index.set_watchdog(False)
