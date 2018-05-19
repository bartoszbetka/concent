import datetime
import copy

from base64 import b64encode
from typing import List
from typing import Optional
from typing import Union

from django.conf import settings
from django.core.mail import mail_admins
from django.http import HttpResponse
from django.utils import timezone

from constance import config

from golem_sci.events import BatchTransferEvent
from golem_sci.events import ForcedPaymentEvent
from golem_messages import message
from golem_messages.message import FileTransferToken
from golem_messages.message.tasks import SubtaskResultsRejected

from core.exceptions import ConcentInSoftShutdownMode
from core.exceptions import Http400
from core.models import Client
from core.models import PaymentInfo
from core.models import PendingResponse
from core.models import StoredMessage
from core.models import Subtask
from core.payments import base
from core.payments.sci_backend import TransactionType
from core.queue_operations import send_blender_verification_request
from core.subtask_helpers import verify_message_subtask_results_accepted
from core.transfer_operations import store_pending_message
from core.transfer_operations import create_file_transfer_token_for_golem_client
from core.validation import validate_all_messages_identical
from core.validation import validate_ethereum_addresses
from core.validation import validate_golem_message_signed_with_key
from core.validation import validate_golem_message_subtask_results_rejected
from core.validation import validate_report_computed_task_time_window
from core.validation import validate_task_to_compute
from utils import logging
from utils.constants import ErrorCode
from utils.helpers import calculate_maximum_download_time
from utils.helpers import calculate_subtask_verification_time
from utils.helpers import deserialize_message
from utils.helpers import get_current_utc_timestamp
from utils.helpers import parse_timestamp_to_utc_datetime
from utils.helpers import sign_message


def handle_send_force_report_computed_task(client_message):
    task_to_compute = client_message.report_computed_task.task_to_compute
    provider_public_key = task_to_compute.provider_public_key
    requestor_public_key = task_to_compute.requestor_public_key

    validate_task_to_compute(task_to_compute)
    validate_report_computed_task_time_window(client_message.report_computed_task)
    validate_golem_message_signed_with_key(
        task_to_compute,
        requestor_public_key,
    )

    if Subtask.objects.filter(
        subtask_id = client_message.report_computed_task.task_to_compute.compute_task_def['subtask_id'],
    ).exists():
        raise Http400(
            "{} is already being processed for this task.".format(client_message.__class__.__name__),
            error_code=ErrorCode.SUBTASK_DUPLICATE_REQUEST,
        )

    if client_message.report_computed_task.task_to_compute.compute_task_def['deadline'] < get_current_utc_timestamp():
        logging.log_timeout(
            client_message,
            provider_public_key,
            client_message.report_computed_task.task_to_compute.compute_task_def['deadline'],
        )
        return message.concents.ForceReportComputedTaskResponse(
            reason=message.concents.ForceReportComputedTaskResponse.REASON.SubtaskTimeout
        )

    subtask = store_subtask(
        task_id              = task_to_compute.compute_task_def['task_id'],
        subtask_id           = task_to_compute.compute_task_def['subtask_id'],
        provider_public_key  = provider_public_key,
        requestor_public_key = requestor_public_key,
        state                = Subtask.SubtaskState.FORCING_REPORT,
        next_deadline        = task_to_compute.compute_task_def['deadline'] + settings.CONCENT_MESSAGING_TIME,
        task_to_compute      = task_to_compute,
        report_computed_task = client_message.report_computed_task,
    )
    store_pending_message(
        response_type       = PendingResponse.ResponseType.ForceReportComputedTask,
        client_public_key   = requestor_public_key,
        queue               = PendingResponse.Queue.Receive,
        subtask             = subtask,
    )
    logging.log_message_added_to_queue(
        client_message,
        provider_public_key,
    )
    return HttpResponse("", status = 202)


def handle_send_ack_report_computed_task(client_message):
    task_to_compute = client_message.report_computed_task.task_to_compute
    report_computed_task = client_message.report_computed_task
    provider_public_key = task_to_compute.provider_public_key
    requestor_public_key = task_to_compute.requestor_public_key

    validate_task_to_compute(task_to_compute)
    validate_golem_message_signed_with_key(
        task_to_compute,
        requestor_public_key,
    )

    if get_current_utc_timestamp() <= task_to_compute.compute_task_def['deadline'] + settings.CONCENT_MESSAGING_TIME:
        try:
            subtask = Subtask.objects.get(
                subtask_id = task_to_compute.compute_task_def['subtask_id'],
            )
        except Subtask.DoesNotExist:
            raise Http400(
                "'ForceReportComputedTask' for this subtask_id has not been initiated yet. Can't accept your 'AckReportComputedTask'.",
                error_code=ErrorCode.QUEUE_COMMUNICATION_NOT_STARTED,
            )

        if subtask.state_enum != Subtask.SubtaskState.FORCING_REPORT:
            raise Http400(
                "Subtask state is {} instead of FORCING_REPORT. Can't accept your 'AckReportComputedTask'.".format(
                    subtask.state
                ),
                error_code=ErrorCode.QUEUE_WRONG_STATE,
            )

        if subtask.report_computed_task.subtask_id != task_to_compute.compute_task_def['subtask_id']:
            raise Http400(
                "Received subtask_id does not match one in related ReportComputedTask. Can't accept your 'AckReportComputedTask'.",
                error_code=ErrorCode.QUEUE_SUBTASK_ID_MISMATCH,
            )

        if subtask.requestor.public_key_bytes != task_to_compute.requestor_public_key:
            raise Http400(
                "Subtask requestor key does not match current client key. Can't accept your 'AckReportComputedTask'.",
                error_code=ErrorCode.QUEUE_REQUESTOR_PUBLIC_KEY_MISMATCH,
            )

        if subtask.ack_report_computed_task_id is not None or subtask.reject_report_computed_task_id is not None:
            raise Http400(
                "Received AckReportComputedTask but RejectReportComputedTask "
                "or another AckReportComputedTask for this task has already been submitted.",
                error_code=ErrorCode.SUBTASK_DUPLICATE_REQUEST,
            )
        validate_all_messages_identical([
            task_to_compute,
            deserialize_message(subtask.task_to_compute.data.tobytes()),
        ])
        new_report_computed_task = None
        try:
            validate_all_messages_identical([
                report_computed_task,
                deserialize_message(subtask.report_computed_task.data.tobytes()),
            ])
        except Http400:
            new_report_computed_task = report_computed_task

        subtask = update_subtask(
            subtask                     = subtask,
            state                       = Subtask.SubtaskState.REPORTED,
            next_deadline               = None,
            set_next_deadline           = True,
            ack_report_computed_task    = client_message,
            report_computed_task        = new_report_computed_task,
        )
        store_pending_message(
            response_type       = PendingResponse.ResponseType.ForceReportComputedTaskResponse,
            client_public_key   = provider_public_key,
            queue               = PendingResponse.Queue.Receive,
            subtask             = subtask,
        )
        logging.log_message_added_to_queue(
            client_message,
            requestor_public_key,
        )
        return HttpResponse("", status = 202)
    else:
        logging.log_timeout(
            client_message,
            requestor_public_key,
            task_to_compute.compute_task_def['deadline'] + settings.CONCENT_MESSAGING_TIME,
        )
        raise Http400(
            "Time to acknowledge this task is already over.",
            error_code=ErrorCode.QUEUE_TIMEOUT,
        )


def handle_send_reject_report_computed_task(client_message):
    if (
        isinstance(client_message.cannot_compute_task, message.CannotComputeTask) and
        isinstance(client_message.task_failure, message.TaskFailure)
    ):
        raise Http400(
            "RejectReportComputedTask cannot contain CannotComputeTask and TaskFailure at the same time.",
            error_code=ErrorCode.MESSAGE_INVALID,
        )

    # Validate if task_to_compute is valid instance of TaskToCompute.
    task_to_compute = client_message.task_to_compute
    validate_task_to_compute(task_to_compute)

    provider_public_key = task_to_compute.provider_public_key
    requestor_public_key = task_to_compute.requestor_public_key

    # Validate if TaskToCompute signed by the requestor.
    validate_golem_message_signed_with_key(
        task_to_compute,
        requestor_public_key,
    )

    # If reason is GotMessageCannotComputeTask,
    # cannot_compute_task is instance of CannotComputeTask signed by the provider.
    if client_message.reason == message.RejectReportComputedTask.REASON.GotMessageCannotComputeTask:
        if not isinstance(client_message.cannot_compute_task, message.CannotComputeTask):
            raise Http400(
                "Expected CannotComputeTask inside RejectReportComputedTask.",
                error_code=ErrorCode.MESSAGE_INVALID,
            )
        validate_task_to_compute(client_message.cannot_compute_task.task_to_compute)
        validate_golem_message_signed_with_key(
            client_message.cannot_compute_task,
            provider_public_key,
        )

    # If reason is GotMessageTaskFailure,
    # task_failure is instance of TaskFailure signed by the provider.
    elif client_message.reason == message.RejectReportComputedTask.REASON.GotMessageTaskFailure:
        if not isinstance(client_message.task_failure, message.TaskFailure):
            raise Http400(
                "Expected TaskFailure inside RejectReportComputedTask.",
                error_code=ErrorCode.MESSAGE_INVALID,
            )
        validate_task_to_compute(client_message.task_failure.task_to_compute)
        validate_golem_message_signed_with_key(
            client_message.task_failure,
            provider_public_key,
        )

    # RejectReportComputedTask should contain empty cannot_compute_task and task_failure
    else:
        if client_message.cannot_compute_task is not None or client_message.task_failure is not None:
            raise Http400(
                "RejectReportComputedTask requires empty 'cannot_compute_task' and 'task_failure' with {} reason.".format(
                    client_message.reason.name
                ),
                error_code=ErrorCode.MESSAGE_INVALID,
            )

    try:
        subtask = Subtask.objects.get(
            subtask_id = task_to_compute.compute_task_def['subtask_id'],
        )
    except Subtask.DoesNotExist:
        raise Http400(
            "'ForceReportComputedTask' for this task and client combination has not been initiated yet. Can't accept your 'RejectReportComputedTask'.",
            error_code=ErrorCode.QUEUE_COMMUNICATION_NOT_STARTED,
        )

    if subtask.state_enum != Subtask.SubtaskState.FORCING_REPORT:
        raise Http400(
            "Subtask state is {} instead of FORCING_REPORT. Can't accept your 'RejectReportComputedTask'.".format(
                subtask.state
            ),
            error_code=ErrorCode.QUEUE_WRONG_STATE,
        )

    if subtask.report_computed_task.subtask_id != task_to_compute.compute_task_def['subtask_id']:
        raise Http400(
            "Received subtask_id does not match one in related ReportComputedTask. Can't accept your 'RejectReportComputedTask'.",
            error_code=ErrorCode.QUEUE_SUBTASK_ID_MISMATCH,
        )

    if subtask.requestor.public_key_bytes != requestor_public_key:
        raise Http400(
            "Subtask requestor key does not match current client key. Can't accept your 'RejectReportComputedTask'.",
            error_code=ErrorCode.QUEUE_REQUESTOR_PUBLIC_KEY_MISMATCH,
        )

    validate_all_messages_identical(
        [
            task_to_compute,
            deserialize_message(subtask.task_to_compute.data.tobytes()),
        ]
    )

    if client_message.reason == message.RejectReportComputedTask.REASON.SubtaskTimeLimitExceeded:

        subtask = update_subtask(
            subtask                     = subtask,
            state                       = Subtask.SubtaskState.REPORTED,
            next_deadline               = None,
            set_next_deadline           = True,
            reject_report_computed_task = client_message,
        )
        store_pending_message(
            response_type       = PendingResponse.ResponseType.ForceReportComputedTaskResponse,
            client_public_key   = provider_public_key,
            queue               = PendingResponse.Queue.Receive,
            subtask             = subtask,
        )
        store_pending_message(
            response_type       = PendingResponse.ResponseType.VerdictReportComputedTask,
            client_public_key   = subtask.requestor.public_key_bytes,
            queue               = PendingResponse.Queue.ReceiveOutOfBand,
            subtask             = subtask,
        )
        logging.log_message_added_to_queue(
            client_message,
            requestor_public_key,
        )
        return HttpResponse("", status = 202)

    deserialized_message = deserialize_message(subtask.task_to_compute.data.tobytes())

    if get_current_utc_timestamp() <= deserialized_message.compute_task_def['deadline'] + settings.CONCENT_MESSAGING_TIME:
        if subtask.ack_report_computed_task_id is not None or subtask.ack_report_computed_task_id is not None:
            raise Http400(
                "Received RejectReportComputedTask but AckReportComputedTask or another RejectReportComputedTask for this task has already been submitted.",
                error_code=ErrorCode.SUBTASK_DUPLICATE_REQUEST,
            )

        subtask = update_subtask(
            subtask                     = subtask,
            state                       = Subtask.SubtaskState.FAILED,
            next_deadline               = None,
            set_next_deadline           = True,
            reject_report_computed_task = client_message,
        )
        store_pending_message(
            response_type       = PendingResponse.ResponseType.ForceReportComputedTaskResponse,
            client_public_key   = provider_public_key,
            queue               = PendingResponse.Queue.Receive,
            subtask             = subtask,
        )
        logging.log_message_added_to_queue(
            client_message,
            requestor_public_key,
        )
        return HttpResponse("", status = 202)
    else:
        logging.log_timeout(
            client_message,
            requestor_public_key,
            deserialized_message.compute_task_def['deadline'] + settings.CONCENT_MESSAGING_TIME,
        )
        raise Http400(
            "Time to acknowledge this task is already over.",
            error_code=ErrorCode.QUEUE_TIMEOUT,
        )


def handle_send_force_get_task_result(client_message: message.concents.ForceGetTaskResult) -> message.concents:
    task_to_compute = client_message.report_computed_task.task_to_compute
    provider_public_key = task_to_compute.provider_public_key
    requestor_public_key = task_to_compute.requestor_public_key

    validate_task_to_compute(task_to_compute)
    validate_golem_message_signed_with_key(
        task_to_compute,
        requestor_public_key,
    )
    force_get_task_result_deadline = (
        client_message.report_computed_task.task_to_compute.compute_task_def['deadline'] +
        2 * settings.CONCENT_MESSAGING_TIME +
        calculate_maximum_download_time(client_message.report_computed_task.size)
    )

    if Subtask.objects.filter(
        subtask_id = task_to_compute.compute_task_def['subtask_id'],
        state      = Subtask.SubtaskState.FORCING_RESULT_TRANSFER.name,  # pylint: disable=no-member
    ).exists():
        return message.concents.ServiceRefused(
            reason = message.concents.ServiceRefused.REASON.DuplicateRequest,
        )

    elif force_get_task_result_deadline < get_current_utc_timestamp():
        logging.log_timeout(
            client_message,
            requestor_public_key,
            force_get_task_result_deadline,
        )
        return message.concents.ForceGetTaskResultRejected(
            reason    = message.concents.ForceGetTaskResultRejected.REASON.AcceptanceTimeLimitExceeded,
        )

    else:
        subtask = store_or_update_subtask(
            task_id                     = task_to_compute.compute_task_def['task_id'],
            subtask_id                  = task_to_compute.compute_task_def['subtask_id'],
            provider_public_key         = provider_public_key,
            requestor_public_key        = requestor_public_key,
            state                       = Subtask.SubtaskState.FORCING_RESULT_TRANSFER,
            next_deadline               = client_message.report_computed_task.timestamp + settings.FORCE_ACCEPTANCE_TIME + settings.CONCENT_MESSAGING_TIME,
            set_next_deadline           = True,
            report_computed_task        = client_message.report_computed_task,
            task_to_compute             = task_to_compute,
        )
        store_pending_message(
            response_type       = PendingResponse.ResponseType.ForceGetTaskResultUpload,
            client_public_key   = provider_public_key,
            queue               = PendingResponse.Queue.Receive,
            subtask             = subtask,
        )
        return message.concents.AckForceGetTaskResult(
            force_get_task_result = client_message,
        )


def handle_send_force_subtask_results(client_message: message.concents.ForceSubtaskResults):
    task_to_compute = client_message.ack_report_computed_task.report_computed_task.task_to_compute
    provider_public_key = task_to_compute.provider_public_key
    requestor_public_key = task_to_compute.requestor_public_key

    validate_task_to_compute(task_to_compute)
    validate_golem_message_signed_with_key(
        task_to_compute,
        requestor_public_key,
    )

    current_time = get_current_utc_timestamp()

    if Subtask.objects.filter(
        subtask_id = task_to_compute.compute_task_def['subtask_id'],
        state      = Subtask.SubtaskState.FORCING_ACCEPTANCE.name,  # pylint: disable=no-member
    ).exists():
        return message.concents.ServiceRefused(
            reason = message.concents.ServiceRefused.REASON.DuplicateRequest,
        )

    if not base.is_account_status_positive(  # pylint: disable=no-value-for-parameter
        client_eth_address      = client_message.ack_report_computed_task.report_computed_task.task_to_compute.requestor_ethereum_address,
        pending_value           = client_message.ack_report_computed_task.report_computed_task.task_to_compute.price,
    ):
        return message.concents.ServiceRefused(
            reason      = message.concents.ServiceRefused.REASON.TooSmallRequestorDeposit,
        )

    base.make_force_payment_to_provider(  # pylint: disable=no-value-for-parameter
        requestor_eth_address = client_message.ack_report_computed_task.report_computed_task.task_to_compute.requestor_ethereum_address,
        provider_eth_address = client_message.ack_report_computed_task.report_computed_task.task_to_compute.provider_ethereum_address,
        value = client_message.ack_report_computed_task.report_computed_task.task_to_compute.price,
        payment_ts = current_time,
    )

    verification_deadline       = (
        client_message.ack_report_computed_task.report_computed_task.task_to_compute.compute_task_def['deadline'] +
        calculate_subtask_verification_time(client_message.ack_report_computed_task.report_computed_task)
    )
    forcing_acceptance_deadline = (
        client_message.ack_report_computed_task.report_computed_task.task_to_compute.compute_task_def['deadline'] +
        calculate_subtask_verification_time(client_message.ack_report_computed_task.report_computed_task) +
        settings.FORCE_ACCEPTANCE_TIME
    )
    if forcing_acceptance_deadline < current_time:
        logging.log_timeout(
            client_message,
            provider_public_key,
            forcing_acceptance_deadline,
        )
        return message.concents.ForceSubtaskResultsRejected(
            reason = message.concents.ForceSubtaskResultsRejected.REASON.RequestTooLate,
        )
    elif current_time < verification_deadline:
        logging.log_timeout(
            client_message,
            provider_public_key,
            verification_deadline,
        )
        return message.concents.ForceSubtaskResultsRejected(
            reason = message.concents.ForceSubtaskResultsRejected.REASON.RequestPremature,
        )
    else:
        subtask = store_or_update_subtask(
            task_id                     = task_to_compute.compute_task_def['task_id'],
            subtask_id                  = task_to_compute.compute_task_def['subtask_id'],
            provider_public_key         = provider_public_key,
            requestor_public_key        = requestor_public_key,
            state                       = Subtask.SubtaskState.FORCING_ACCEPTANCE,
            next_deadline               = forcing_acceptance_deadline + settings.CONCENT_MESSAGING_TIME,
            set_next_deadline           = True,
            ack_report_computed_task    = client_message.ack_report_computed_task,
            task_to_compute             = client_message.ack_report_computed_task.report_computed_task.task_to_compute,
            report_computed_task        = client_message.ack_report_computed_task.report_computed_task,
        )
        store_pending_message(
            response_type       = PendingResponse.ResponseType.ForceSubtaskResults,
            client_public_key   = requestor_public_key,
            queue               = PendingResponse.Queue.Receive,
            subtask             = subtask,
        )
        logging.log_message_added_to_queue(
            client_message,
            provider_public_key,
        )
        return HttpResponse("", status = 202)


def handle_send_force_subtask_results_response(client_message):
    if isinstance(client_message.subtask_results_accepted, message.tasks.SubtaskResultsAccepted):
        task_to_compute           = client_message.subtask_results_accepted.task_to_compute
        subtask_results_accepted  = client_message.subtask_results_accepted
        subtask_results_rejected  = None
        state                     = Subtask.SubtaskState.ACCEPTED
        response_type             = PendingResponse.ResponseType.ForceSubtaskResultsResponse
    else:
        task_to_compute           = client_message.subtask_results_rejected.report_computed_task.task_to_compute
        subtask_results_accepted  = None
        subtask_results_rejected  = client_message.subtask_results_rejected
        state                     = Subtask.SubtaskState.REJECTED
        response_type             = PendingResponse.ResponseType.SubtaskResultsRejected

    validate_task_to_compute(task_to_compute)
    provider_public_key = task_to_compute.provider_public_key
    requestor_public_key = task_to_compute.requestor_public_key

    validate_golem_message_signed_with_key(
        task_to_compute,
        requestor_public_key,
    )

    try:
        subtask = Subtask.objects.get(
            subtask_id = task_to_compute.compute_task_def['subtask_id'],
        )
    except Subtask.DoesNotExist:
        raise Http400(
            "'ForceSubtaskResults' for this subtask has not been initiated yet. Can't accept your '{}'.".format(
                client_message.TYPE
            ),
            error_code=ErrorCode.QUEUE_COMMUNICATION_NOT_STARTED,
        )

    if subtask.state_enum != Subtask.SubtaskState.FORCING_ACCEPTANCE:
        raise Http400(
            "Subtask state is {} instead of FORCING_ACCEPTANCE. Can't accept your '{}'.".format(
                subtask.state,
                client_message.TYPE,
            ),
            error_code=ErrorCode.QUEUE_WRONG_STATE,
        )

    if subtask.requestor.public_key_bytes != requestor_public_key:
        raise Http400(
            "Subtask requestor key does not match current client key.  Can't accept your '{}'.".format(
                client_message.TYPE
            ),
            error_code=ErrorCode.QUEUE_REQUESTOR_PUBLIC_KEY_MISMATCH,
        )

    if subtask.subtask_results_accepted_id is not None or subtask.subtask_results_rejected_id is not None:
        raise Http400(
            "This subtask has been resolved already.",
            error_code=ErrorCode.SUBTASK_DUPLICATE_REQUEST,
        )

    validate_all_messages_identical([
        task_to_compute,
        deserialize_message(subtask.task_to_compute.data.tobytes()),
    ])

    subtask = update_subtask(
        subtask                     = subtask,
        state                       = state,
        next_deadline               = None,
        set_next_deadline           = True,
        subtask_results_accepted    = subtask_results_accepted,
        subtask_results_rejected    = subtask_results_rejected,
    )
    store_pending_message(
        response_type       = response_type,
        client_public_key   = provider_public_key,
        queue               = PendingResponse.Queue.Receive,
        subtask             = subtask,
    )
    logging.log_message_added_to_queue(
        client_message,
        requestor_public_key,
    )
    return HttpResponse("", status = 202)


def sum_payments(payments: List[Union[ForcedPaymentEvent, BatchTransferEvent]]):
    assert isinstance(payments, list)

    return sum([item.amount for item in payments])


def sum_subtask_price(subtask_results_accepted_list: List[message.tasks.SubtaskResultsAccepted]):
    assert isinstance(subtask_results_accepted_list, list)

    return sum([subtask_results_accepted.task_to_compute.price for subtask_results_accepted in subtask_results_accepted_list])


def sum_amount_price_for_provider(
    list_of_forced_payments:        List[ForcedPaymentEvent],
    list_of_payments:               List[BatchTransferEvent],
    subtask_results_accepted_list:  List[message.tasks.SubtaskResultsAccepted],
):
    assert isinstance(list_of_payments,                 list)
    assert isinstance(list_of_forced_payments,          list)
    assert isinstance(subtask_results_accepted_list,    list)

    force_payments_price    = sum_payments(list_of_forced_payments)
    payments_price          = sum_payments(list_of_payments)
    subtasks_price          = sum_subtask_price(subtask_results_accepted_list)

    amount_paid     = payments_price + force_payments_price
    amount_pending  = subtasks_price - amount_paid

    return (amount_paid, amount_pending)


def get_clients_eth_accounts(task_to_compute: message.tasks.TaskToCompute):
    assert isinstance(task_to_compute, message.tasks.TaskToCompute)

    requestor_eth_address   = task_to_compute.requestor_ethereum_address
    provider_eth_address    = task_to_compute.provider_ethereum_address

    return (requestor_eth_address, provider_eth_address)


def handle_send_force_payment(client_message: message.concents.ForcePayment) -> message.concents.ForcePaymentCommitted:  # pylint: disable=inconsistent-return-statements

    # Concent should not accept payment requests in soft shutdown mode.
    if config.SOFT_SHUTDOWN_MODE is True:
        raise ConcentInSoftShutdownMode

    current_time            = get_current_utc_timestamp()

    if not verify_message_subtask_results_accepted(client_message.subtask_results_accepted_list):
        return message.concents.ServiceRefused(
            reason = message.concents.ServiceRefused.REASON.InvalidRequest
        )

    if not are_ids_unique_in_subtask_results_accepted_list(client_message.subtask_results_accepted_list):
        return message.concents.ServiceRefused(
            reason = message.concents.ServiceRefused.REASON.DuplicateRequest
        )

    for subtask_results_accepted in client_message.subtask_results_accepted_list:
        validate_golem_message_signed_with_key(
            subtask_results_accepted.task_to_compute,
            subtask_results_accepted.task_to_compute.requestor_public_key,
        )
    task_to_compute = client_message.subtask_results_accepted_list[0].task_to_compute
    (requestor_eth_address, provider_eth_address) = get_clients_eth_accounts(task_to_compute)
    validate_ethereum_addresses(requestor_eth_address, provider_eth_address)
    requestor_ethereum_public_key = task_to_compute.requestor_ethereum_public_key

    # Concent defines time T0 equal to oldest payment_ts from passed SubtaskResultAccepted messages from subtask_results_accepted_list.
    oldest_payments_ts = min(
        subtask_results_accepted.payment_ts for subtask_results_accepted in client_message.subtask_results_accepted_list
    )

    # Concent gets list of transactions from payment API where timestamp >= T0.
    list_of_transactions = base.get_list_of_payments(  # pylint: disable=no-value-for-parameter
        requestor_eth_address   = requestor_eth_address,
        provider_eth_address    = provider_eth_address,
        payment_ts              = oldest_payments_ts,
        current_time            = current_time,
        transaction_type        = TransactionType.BATCH,
    )

    # Concent defines time T1 equal to youngest timestamp from list of transactions.
    if not len(list_of_transactions) == 0:
        youngest_transaction = max(transaction.closure_time for transaction in list_of_transactions)

        # Concent checks if all passed SubtaskResultAccepted messages from subtask_results_accepted_list have payment_ts < T1
        T1_is_bigger_than_payments_ts = any(youngest_transaction > subtask_results_accepted.payment_ts for subtask_results_accepted in client_message.subtask_results_accepted_list)
    else:
        T1_is_bigger_than_payments_ts = None

    # Any of the items from list of overdue acceptances matches condition current_time < payment_ts + PAYMENT_DUE_TIME
    acceptance_time_overdue = any(current_time < subtask_results_accepted.payment_ts + settings.PAYMENT_DUE_TIME for subtask_results_accepted in client_message.subtask_results_accepted_list)

    if T1_is_bigger_than_payments_ts or acceptance_time_overdue:
        return message.concents.ForcePaymentRejected(
            reason = message.concents.ForcePaymentRejected.REASON.TimestampError
        )

    # Concent gets list of forced payments from payment API where T0 <= payment_ts + PAYMENT_DUE_TIME.
    list_of_forced_payments = base.get_list_of_payments(  # pylint: disable=no-value-for-parameter
        requestor_eth_address   = requestor_eth_address,
        provider_eth_address    = provider_eth_address,
        payment_ts              = oldest_payments_ts + settings.PAYMENT_DUE_TIME,  # Im not sure, check it please
        current_time            = current_time,
        transaction_type        = TransactionType.FORCE,
    )

    (amount_paid, amount_pending) = sum_amount_price_for_provider(
        list_of_forced_payments         = list_of_forced_payments,
        list_of_payments                = list_of_transactions,
        subtask_results_accepted_list   = client_message.subtask_results_accepted_list,
    )

    # Concent defines time T2 (end time) equal to youngest payment_ts from passed SubtaskResultAccepted messages from subtask_results_accepted_list.
    payment_ts = min(
        subtask_results_accepted.payment_ts for subtask_results_accepted in client_message.subtask_results_accepted_list
    )

    if amount_pending <= 0:
        return message.concents.ForcePaymentRejected(
            reason = message.concents.ForcePaymentRejected.REASON.NoUnsettledTasksFound
        )
    elif amount_pending > 0:
        base.make_force_payment_to_provider(  # pylint: disable=no-value-for-parameter
            requestor_eth_address   = requestor_eth_address,
            provider_eth_address    = provider_eth_address,
            value                   = amount_pending,
            payment_ts              = current_time,
        )

        provider_force_payment_commited = message.concents.ForcePaymentCommitted(
            payment_ts              = payment_ts,
            task_owner_key          = requestor_ethereum_public_key,
            provider_eth_account    = provider_eth_address,
            amount_paid             = amount_paid,
            amount_pending          = amount_pending,
            recipient_type          = message.concents.ForcePaymentCommitted.Actor.Provider,
        )

        requestor_force_payment_commited = message.concents.ForcePaymentCommitted(
            payment_ts              = payment_ts,
            task_owner_key          = requestor_ethereum_public_key,
            provider_eth_account    = provider_eth_address,
            amount_paid             = amount_paid,
            amount_pending          = amount_pending,
            recipient_type          = message.concents.ForcePaymentCommitted.Actor.Requestor,
        )

        store_pending_message(
            response_type       = PendingResponse.ResponseType.ForcePaymentCommitted,
            client_public_key   = task_to_compute.requestor_public_key,
            queue               = PendingResponse.Queue.ReceiveOutOfBand,
            payment_message     = requestor_force_payment_commited
        )

        provider_force_payment_commited.sig = None
        return provider_force_payment_commited


def handle_unsupported_golem_messages_type(client_message):
    if hasattr(client_message, 'TYPE'):
        raise Http400(
            "This message type ({}) is either not supported or cannot be submitted to Concent.".format(client_message.TYPE),
            error_code=ErrorCode.MESSAGE_UNEXPECTED,
        )
    else:
        raise Http400(
            "Unknown message type or not a Golem message.",
            error_code=ErrorCode.MESSAGE_UNKNOWN,
        )


def store_subtask(
    task_id:                        str,
    subtask_id:                     str,
    provider_public_key:            bytes,
    requestor_public_key:           bytes,
    state:                          Subtask.SubtaskState,
    next_deadline:                  Optional[int],
    task_to_compute:                message.TaskToCompute,
    report_computed_task:           message.ReportComputedTask,
    ack_report_computed_task:       Optional[message.AckReportComputedTask]        = None,
    reject_report_computed_task:    Optional[message.RejectReportComputedTask]     = None,
    subtask_results_accepted:       Optional[message.tasks.SubtaskResultsAccepted] = None,
    subtask_results_rejected:       Optional[message.tasks.SubtaskResultsRejected] = None,
):
    """
    Validates and stores subtask and its data in Subtask table.
    Stores related messages in StoredMessage table and adds relation to newly created subtask.
    """
    assert isinstance(task_id,              str)
    assert isinstance(subtask_id,           str)
    assert isinstance(provider_public_key,  bytes)
    assert isinstance(requestor_public_key, bytes)
    assert isinstance(task_to_compute, message.TaskToCompute)
    assert isinstance(report_computed_task, message.ReportComputedTask)
    assert state in Subtask.SubtaskState
    assert (state in Subtask.ACTIVE_STATES)  == (isinstance(next_deadline, int))
    assert (state in Subtask.PASSIVE_STATES) == (next_deadline is None)

    provider  = Client.objects.get_or_create_full_clean(provider_public_key)
    requestor = Client.objects.get_or_create_full_clean(requestor_public_key)

    subtask = Subtask(
        task_id         = task_id,
        subtask_id      = subtask_id,
        provider        = provider,
        requestor       = requestor,
        state           = state.name,
        next_deadline   = parse_timestamp_to_utc_datetime(next_deadline) if next_deadline is not None else None,
        task_to_compute=store_message(task_to_compute, task_id, subtask_id),
        report_computed_task=store_message(report_computed_task, task_id, subtask_id),
    )

    set_subtask_messages(
        subtask,
        ack_report_computed_task    = ack_report_computed_task,
        reject_report_computed_task = reject_report_computed_task,
        subtask_results_accepted    = subtask_results_accepted,
        subtask_results_rejected    = subtask_results_rejected,
    )

    subtask.full_clean()
    subtask.save()

    logging.log_subtask_stored(
        task_id,
        subtask_id,
        state.name,
        provider_public_key,
        requestor_public_key,
        next_deadline,
    )

    return subtask


def handle_messages_from_database(
    client_public_key:  bytes                   = None,
    response_type:      PendingResponse.Queue   = None,
):
    assert client_public_key    not in ['', None]

    encoded_client_public_key = b64encode(client_public_key)
    pending_response = PendingResponse.objects.filter(
        client__public_key = encoded_client_public_key,
        queue              = response_type.name,
        delivered          = False,
    ).order_by('created_at').first()

    if pending_response is None:
        return None

    assert pending_response.response_type_enum in set(PendingResponse.ResponseType)

    if pending_response.response_type == PendingResponse.ResponseType.ForceReportComputedTask.name:  # pylint: disable=no-member
        report_computed_task = deserialize_message(pending_response.subtask.report_computed_task.data.tobytes())
        response_to_client = message.concents.ForceReportComputedTask(
            report_computed_task = report_computed_task
        )
        mark_message_as_delivered_and_log(pending_response, response_to_client)
        return response_to_client

    elif pending_response.response_type == PendingResponse.ResponseType.ForceReportComputedTaskResponse.name:  # pylint: disable=no-member
        if pending_response.subtask.ack_report_computed_task is not None:
            ack_report_computed_task = deserialize_message(pending_response.subtask.ack_report_computed_task.data.tobytes())
            response_to_client = message.concents.ForceReportComputedTaskResponse(
                ack_report_computed_task = ack_report_computed_task,
            )
            mark_message_as_delivered_and_log(pending_response, response_to_client)
            return response_to_client

        elif pending_response.subtask.reject_report_computed_task is not None:
            reject_report_computed_task = deserialize_message(pending_response.subtask.reject_report_computed_task.data.tobytes())
            response_to_client          = message.concents.ForceReportComputedTaskResponse(
                reject_report_computed_task = reject_report_computed_task,
            )
            if reject_report_computed_task.reason == message.RejectReportComputedTask.REASON.SubtaskTimeLimitExceeded:

                ack_report_computed_task = message.AckReportComputedTask(
                    report_computed_task = deserialize_message(pending_response.subtask.report_computed_task.data.tobytes()),
                )
                sign_message(ack_report_computed_task, settings.CONCENT_PRIVATE_KEY)
                response_to_client.ack_report_computed_task = ack_report_computed_task
            mark_message_as_delivered_and_log(pending_response, response_to_client)
            return response_to_client
        else:
            ack_report_computed_task = message.AckReportComputedTask(
                report_computed_task=deserialize_message(pending_response.subtask.report_computed_task.data.tobytes()),
            )
            sign_message(ack_report_computed_task, settings.CONCENT_PRIVATE_KEY)
            response_to_client = message.concents.ForceReportComputedTaskResponse(
                ack_report_computed_task = ack_report_computed_task,
            )
            mark_message_as_delivered_and_log(pending_response, response_to_client)
            return response_to_client

    elif pending_response.response_type == PendingResponse.ResponseType.VerdictReportComputedTask.name:  # pylint: disable=no-member
        ack_report_computed_task = message.AckReportComputedTask(
            report_computed_task=deserialize_message(pending_response.subtask.report_computed_task.data.tobytes()),
        )
        sign_message(ack_report_computed_task, settings.CONCENT_PRIVATE_KEY)
        report_computed_task     = deserialize_message(pending_response.subtask.report_computed_task.data.tobytes())
        response_to_client = message.concents.VerdictReportComputedTask(
            ack_report_computed_task    = ack_report_computed_task,
            force_report_computed_task  = message.concents.ForceReportComputedTask(
                report_computed_task = report_computed_task,
            ),
        )
        mark_message_as_delivered_and_log(pending_response, response_to_client)
        return response_to_client

    elif pending_response.response_type == PendingResponse.ResponseType.ForceGetTaskResultRejected.name:  # pylint: disable=no-member
        report_computed_task = deserialize_message(pending_response.subtask.report_computed_task.data.tobytes())
        response_to_client = message.concents.ForceGetTaskResultRejected(
            force_get_task_result = message.concents.ForceGetTaskResult(
                report_computed_task = report_computed_task,
            )
        )
        mark_message_as_delivered_and_log(pending_response, response_to_client)
        return response_to_client

    elif pending_response.response_type == PendingResponse.ResponseType.ForceGetTaskResultFailed.name:  # pylint: disable=no-member
        task_to_compute = deserialize_message(pending_response.subtask.task_to_compute.data.tobytes())
        response_to_client = message.concents.ForceGetTaskResultFailed(
            task_to_compute = task_to_compute,
        )
        mark_message_as_delivered_and_log(pending_response, response_to_client)
        return response_to_client

    elif pending_response.response_type == PendingResponse.ResponseType.ForceGetTaskResultUpload.name:  # pylint: disable=no-member
        report_computed_task    = deserialize_message(pending_response.subtask.report_computed_task.data.tobytes())
        file_transfer_token     = create_file_transfer_token_for_golem_client(
            report_computed_task,
            client_public_key,
            FileTransferToken.Operation.upload,
        )

        response_to_client = message.concents.ForceGetTaskResultUpload(
            file_transfer_token     = file_transfer_token,
            force_get_task_result   = message.concents.ForceGetTaskResult(
                report_computed_task = report_computed_task,
            )
        )
        mark_message_as_delivered_and_log(pending_response, response_to_client)
        return response_to_client

    elif pending_response.response_type == PendingResponse.ResponseType.ForceGetTaskResultDownload.name:  # pylint: disable=no-member
        report_computed_task    = deserialize_message(pending_response.subtask.report_computed_task.data.tobytes())
        file_transfer_token     = create_file_transfer_token_for_golem_client(
            report_computed_task,
            client_public_key,
            FileTransferToken.Operation.download,
        )

        response_to_client = message.concents.ForceGetTaskResultDownload(
            file_transfer_token     = file_transfer_token,
            force_get_task_result   = message.concents.ForceGetTaskResult(
                report_computed_task = report_computed_task,
            )
        )
        mark_message_as_delivered_and_log(pending_response, response_to_client)
        return response_to_client

    elif pending_response.response_type == PendingResponse.ResponseType.ForceSubtaskResults.name:  # pylint: disable=no-member
        ack_report_computed_task = deserialize_message(pending_response.subtask.ack_report_computed_task.data.tobytes())
        response_to_client = message.concents.ForceSubtaskResults(
            ack_report_computed_task = ack_report_computed_task
        )
        mark_message_as_delivered_and_log(pending_response, response_to_client)
        return response_to_client

    elif pending_response.response_type == PendingResponse.ResponseType.SubtaskResultsSettled.name:  # pylint: disable=no-member
        task_to_compute = deserialize_message(pending_response.subtask.task_to_compute.data.tobytes())
        response_to_client = message.concents.SubtaskResultsSettled(
            task_to_compute = task_to_compute,
        )
        mark_message_as_delivered_and_log(pending_response, response_to_client)
        return response_to_client

    elif pending_response.response_type == PendingResponse.ResponseType.ForceSubtaskResultsResponse.name:  # pylint: disable=no-member
        subtask_results_accepted = deserialize_message(pending_response.subtask.subtask_results_accepted.data.tobytes())
        response_to_client = message.concents.ForceSubtaskResultsResponse(
            subtask_results_accepted = subtask_results_accepted,
        )
        mark_message_as_delivered_and_log(pending_response, response_to_client)
        return response_to_client

    elif pending_response.response_type == PendingResponse.ResponseType.SubtaskResultsRejected.name:  # pylint: disable=no-member
        subtask_results_rejected = deserialize_message(pending_response.subtask.subtask_results_rejected.data.tobytes())
        response_to_client = message.concents.ForceSubtaskResultsResponse(
            subtask_results_rejected = subtask_results_rejected,
        )
        mark_message_as_delivered_and_log(pending_response, response_to_client)
        return response_to_client

    elif pending_response.response_type == PendingResponse.ResponseType.ForcePaymentCommitted.name:  # pylint: disable=no-member
        payment_message = pending_response.payments.filter(
            pending_response__pk = pending_response.pk
        ).order_by('id').last()

        response_to_client = message.concents.ForcePaymentCommitted(
            payment_ts              = datetime.datetime.timestamp(payment_message.payment_ts),
            task_owner_key          = payment_message.task_owner_key,
            provider_eth_account    = payment_message.provider_eth_account,
            amount_paid             = payment_message.amount_paid,
            amount_pending          = payment_message.amount_pending,
        )
        if payment_message.recipient_type == PaymentInfo.RecipientType.Requestor.name:  # pylint: disable=no-member
            response_to_client.recipient_type = message.concents.ForcePaymentCommitted.Actor.Requestor
        elif payment_message.recipient_type == PaymentInfo.RecipientType.Provider.name:  # pylint: disable=no-member
            response_to_client.recipient_type = message.concents.ForcePaymentCommitted.Actor.Provider
        else:
            return None
        mark_message_as_delivered_and_log(pending_response, response_to_client)
        return response_to_client

    else:
        return None


def mark_message_as_delivered_and_log(undelivered_message, log_message):
    undelivered_message.delivered = True
    undelivered_message.full_clean()
    undelivered_message.save()

    logging.log_receive_message_from_database(
        log_message,
        undelivered_message.client.public_key_bytes,
        undelivered_message.response_type,
        undelivered_message.queue
    )


def update_subtask(
    subtask:                        Subtask,
    state:                          Subtask.SubtaskState,
    next_deadline:                  int                                  = None,
    set_next_deadline:              bool                                 = False,
    task_to_compute:                message.TaskToCompute                = None,
    report_computed_task:           message.ReportComputedTask           = None,
    ack_report_computed_task:       message.AckReportComputedTask        = None,
    reject_report_computed_task:    message.RejectReportComputedTask     = None,
    subtask_results_accepted:       message.tasks.SubtaskResultsAccepted = None,
    subtask_results_rejected:       message.tasks.SubtaskResultsRejected = None,
):
    """
    Validates and updates subtask and its data.
    Stores related messages in StoredMessage table and adds relation to newly created subtask.
    """
    assert isinstance(subtask, Subtask)
    assert state in Subtask.SubtaskState
    assert (state in Subtask.ACTIVE_STATES)  == (next_deadline is not None)
    assert (state in Subtask.PASSIVE_STATES) == (next_deadline is None)

    set_subtask_messages(
        subtask,
        task_to_compute             = task_to_compute,
        report_computed_task        = report_computed_task,
        ack_report_computed_task    = ack_report_computed_task,
        reject_report_computed_task = reject_report_computed_task,
        subtask_results_accepted    = subtask_results_accepted,
        subtask_results_rejected    = subtask_results_rejected,
    )

    if set_next_deadline:
        subtask.next_deadline = next_deadline
    subtask.state = state.name
    subtask.full_clean()
    subtask.save()

    logging.log_subtask_updated(
        subtask.task_id,
        subtask.subtask_id,
        state.name,
        subtask.provider.public_key_bytes,
        subtask.requestor.public_key_bytes,
        next_deadline,
    )

    # Concent should send e-mail notification when the last active subtask switches to a passive state.
    if config.SOFT_SHUTDOWN_MODE is True and not Subtask.objects.filter(state__in=Subtask.ACTIVE_STATES).exists():
        mail_admins(
            subject = 'Concent soft shutdown complete',
            message = (
                "All subtasks tracked by this Concent instance are now in passive states.\n"
                "It's safe to turn off the control cluster.\n"
                "Note that there may still be downloads in progress on the storage cluster."
            )
        )

    return subtask


def set_subtask_messages(
    subtask:                        Subtask,
    task_to_compute:                message.TaskToCompute                       = None,
    report_computed_task:           message.ReportComputedTask                  = None,
    ack_report_computed_task:       message.AckReportComputedTask      = None,
    reject_report_computed_task:    message.RejectReportComputedTask   = None,
    subtask_results_accepted:       message.tasks.SubtaskResultsAccepted        = None,
    subtask_results_rejected:       message.tasks.SubtaskResultsRejected        = None,
):
    """
    Stores and adds relation of passed StoredMessages to given subtask.
    If the message name is not present in kwargs, it doesn't do anything with it.
    """
    subtask_messages_to_set = {
        'task_to_compute':              task_to_compute,
        'report_computed_task':         report_computed_task,
        'ack_report_computed_task':     ack_report_computed_task,
        'reject_report_computed_task':  reject_report_computed_task,
        'subtask_results_accepted':     subtask_results_accepted,
        'subtask_results_rejected':     subtask_results_rejected,
    }

    assert set(subtask_messages_to_set).issubset({f.name for f in Subtask._meta.get_fields()})
    assert set(subtask_messages_to_set).issubset(set(Subtask.MESSAGE_FOR_FIELD))

    for message_name, message_type in Subtask.MESSAGE_FOR_FIELD.items():
        message_to_store = subtask_messages_to_set.get(message_name)
        if (
            message_to_store is not None and
            (
                getattr(subtask, message_name) is None or
                message_to_store.__class__ in Subtask.MESSAGE_REPLACEMENT_FOR_STATE[subtask.state_enum]
            )
        ):
            assert isinstance(message_to_store, message_type)
            stored_message = store_message(
                message_to_store,
                subtask.task_id,
                subtask.subtask_id,
            )
            setattr(subtask, message_name, stored_message)
            logging.log_stored_message_added_to_subtask(
                subtask.task_id,
                subtask.subtask_id,
                subtask.state,
                message_type,
            )


def store_or_update_subtask(
    task_id:                        str,
    subtask_id:                     str,
    provider_public_key:            bytes,
    requestor_public_key:           bytes,
    state:                          Subtask.SubtaskState,
    next_deadline:                  int                                  = None,
    set_next_deadline:              bool                                 = False,
    task_to_compute:                message.TaskToCompute                = None,
    report_computed_task:           message.ReportComputedTask           = None,
    ack_report_computed_task:       message.AckReportComputedTask        = None,
    reject_report_computed_task:    message.RejectReportComputedTask     = None,
    subtask_results_accepted:       message.tasks.SubtaskResultsAccepted = None,
    subtask_results_rejected:       message.tasks.SubtaskResultsRejected = None,
):
    try:
        subtask = Subtask.objects.get(
            subtask_id = subtask_id,
        )
    except Subtask.DoesNotExist:
        subtask = None

    if subtask is not None:
        if task_to_compute is not None and subtask.task_to_compute is not None:
            validate_all_messages_identical([
                task_to_compute,
                deserialize_message(subtask.task_to_compute.data.tobytes()),
            ])
        subtask = update_subtask(
            subtask                         = subtask,
            state                           = state,
            next_deadline                   = next_deadline,
            set_next_deadline               = set_next_deadline,
            task_to_compute                 = task_to_compute,
            report_computed_task            = report_computed_task,
            ack_report_computed_task        = ack_report_computed_task,
            reject_report_computed_task     = reject_report_computed_task,
            subtask_results_accepted        = subtask_results_accepted,
            subtask_results_rejected        = subtask_results_rejected,
        )
    else:
        subtask = store_subtask(
            task_id                         = task_id,
            subtask_id                      = subtask_id,
            provider_public_key             = provider_public_key,
            requestor_public_key            = requestor_public_key,
            state                           = state,
            next_deadline                   = next_deadline,
            task_to_compute                 = task_to_compute,
            report_computed_task            = report_computed_task,
            ack_report_computed_task        = ack_report_computed_task,
            reject_report_computed_task     = reject_report_computed_task,
            subtask_results_accepted        = subtask_results_accepted,
            subtask_results_rejected        = subtask_results_rejected,
        )
    return subtask


def store_message(
    golem_message:          message.base.Message,
    task_id:                str,
    subtask_id:             str,
):
    assert golem_message.TYPE in message.registered_message_types

    message_timestamp = datetime.datetime.now(timezone.utc)
    stored_message = StoredMessage(
        type        = golem_message.TYPE,
        timestamp   = message_timestamp,
        data        = copy.copy(golem_message).serialize(),
        task_id     = task_id,
        subtask_id  = subtask_id,
    )
    stored_message.full_clean()
    stored_message.save()

    return stored_message


def handle_send_subtask_results_verify(
    subtask_results_verify: message.concents.SubtaskResultsVerify
):
    subtask_results_rejected = subtask_results_verify.subtask_results_rejected
    report_computed_task = subtask_results_rejected.report_computed_task
    task_to_compute = report_computed_task.task_to_compute
    compute_task_def = task_to_compute.compute_task_def

    requestor_public_key = task_to_compute.requestor_public_key
    provider_public_key = task_to_compute.provider_public_key
    current_time = get_current_utc_timestamp()

    validate_golem_message_subtask_results_rejected(subtask_results_rejected)
    validate_golem_message_signed_with_key(
        task_to_compute,
        requestor_public_key,
    )
    if subtask_results_rejected.reason != SubtaskResultsRejected.REASON.VerificationNegative:
        return message.concents.ServiceRefused(
            reason=message.concents.ServiceRefused.REASON.InvalidRequest,
        )
    if not current_time <= subtask_results_rejected.timestamp + settings.ADDITIONAL_VERIFICATION_CALL_TIME:
        return message.concents.ServiceRefused(
            reason=message.concents.ServiceRefused.REASON.InvalidRequest,
        )
    if not is_signed_by_right_party(
        subtask_results_rejected,
        requestor_public_key,
    ):
        return message.concents.ServiceRefused(
            reason=message.concents.ServiceRefused.REASON.InvalidRequest,
        )
    if Subtask.objects.filter(
        subtask_id=compute_task_def['subtask_id'],
        state__in=[
            Subtask.SubtaskState.VERIFICATION_FILE_TRANSFER.name,  # pylint: disable=no-member
            Subtask.SubtaskState.ADDITIONAL_VERIFICATION.name,     # pylint: disable=no-member
        ]
    ).exists():
        return message.concents.ServiceRefused(
            reason=message.concents.ServiceRefused.REASON.DuplicateRequest,
        )
    if is_message_recieved_in_wrong_state(
        compute_task_def['subtask_id'],
        [
            Subtask.SubtaskState.ACCEPTED.name,  # pylint: disable=no-member
            Subtask.SubtaskState.FAILED.name,  # pylint: disable=no-member
        ]
    ):
        raise Http400(
            "SubtaskResultsVerify is not allowed in current state",
            error_code=ErrorCode.QUEUE_SUBTASK_STATE_TRANSITION_NOT_ALLOWED,
        )
    if not base.is_account_status_positive(  # pylint: disable=no-value-for-parameter
        client_eth_address      = task_to_compute.requestor_ethereum_address,
    ):
        return message.concents.ServiceRefused(
            reason=message.concents.ServiceRefused.REASON.TooSmallRequestorDeposit,
        )

    store_or_update_subtask(
        task_id=compute_task_def['task_id'],
        subtask_id=compute_task_def['subtask_id'],
        provider_public_key=provider_public_key,
        requestor_public_key=requestor_public_key,
        state=Subtask.SubtaskState.VERIFICATION_FILE_TRANSFER,
        next_deadline=subtask_results_rejected.timestamp + settings.ADDITIONAL_VERIFICATION_CALL_TIME,
        set_next_deadline=True,
        task_to_compute=task_to_compute,
        report_computed_task=report_computed_task,
        subtask_results_rejected=subtask_results_rejected,
    )

    send_blender_verification_request()

    encoded_client_public_key = b64encode(provider_public_key)
    ack_subtask_results_verify = message.concents.AckSubtaskResultsVerify(
        subtask_results_verify=subtask_results_verify,
        file_transfer_token=create_file_transfer_token_for_golem_client(
            report_computed_task,
            encoded_client_public_key,
            FileTransferToken.Operation.upload,
            should_add_source=True,
        ),
    )
    return ack_subtask_results_verify


def handle_message(client_message):
    if isinstance(client_message, message.ForceReportComputedTask):
        return handle_send_force_report_computed_task(client_message)

    elif isinstance(client_message, message.AckReportComputedTask):
        return handle_send_ack_report_computed_task(client_message)

    elif isinstance(client_message, message.RejectReportComputedTask):
        return handle_send_reject_report_computed_task(client_message)

    elif (
        isinstance(client_message, message.concents.ForceGetTaskResult) and
        client_message.report_computed_task is not None
    ):
        return handle_send_force_get_task_result(client_message)

    elif (
        isinstance(client_message, message.concents.ForceSubtaskResults) and
        client_message.ack_report_computed_task is not None
    ):
        return handle_send_force_subtask_results(client_message)

    elif (
        isinstance(client_message, message.concents.ForceSubtaskResultsResponse) and
        (client_message.subtask_results_accepted is not None or client_message.subtask_results_rejected is not None)
    ):
        return handle_send_force_subtask_results_response(client_message)

    elif isinstance(client_message, message.concents.ForcePayment):
        return handle_send_force_payment(client_message)

    elif isinstance(client_message, message.concents.SubtaskResultsVerify):
        return handle_send_subtask_results_verify(client_message)

    else:
        return handle_unsupported_golem_messages_type(client_message)


def is_signed_by_right_party(
    subtask_results_rejected: message.tasks.SubtaskResultsRejected,
    other_party_public_key: bytes
) -> bool:
    try:
        validate_golem_message_signed_with_key(
            subtask_results_rejected,
            other_party_public_key,
        )
        return True
    except Http400:
        return False


def is_message_recieved_in_wrong_state(subtask_id, forbidden_states):
    return Subtask.objects.filter(
        subtask_id=subtask_id,
        state__in=forbidden_states
    ).exists()


def are_ids_unique_in_subtask_results_accepted_list(subtask_results_accepted_list):
    subtask_ids = []
    for subtask_results_accepted in subtask_results_accepted_list:
        subtask_ids.append(subtask_results_accepted.subtask_id + ':' + subtask_results_accepted.task_id)
    return len(subtask_ids) == len(set(subtask_ids))
