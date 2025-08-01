import json
import logging
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from sqlalchemy.exc import IntegrityError
from starlette.requests import Request

# NOTE: define permissions before enabling the code block below
from dispatch.auth.permissions import (
    CaseEditPermission,
    CaseJoinPermission,
    CaseViewPermission,
    CaseEventPermission,
    PermissionsDependency,
)
from dispatch.auth.service import CurrentUser
from dispatch.case.enums import CaseStatus
from dispatch.common.utils.views import create_pydantic_include
from dispatch.database.core import DbSession
from dispatch.database.service import CommonParameters, search_filter_sort_paginate
from dispatch.event import flows as event_flows
from dispatch.event.models import EventCreateMinimal, EventUpdate
from dispatch.incident import service as incident_service
from dispatch.incident.models import IncidentCreate, IncidentRead
from dispatch.individual.models import IndividualContactRead
from dispatch.individual.service import get_or_create
from dispatch.models import OrganizationSlug, PrimaryKey
from dispatch.participant.models import ParticipantRead, ParticipantReadMinimal, ParticipantUpdate
from dispatch.project import service as project_service

from .flows import (
    case_add_or_reactivate_participant_flow,
    case_closed_create_flow,
    case_create_conversation_flow,
    case_create_resources_flow,
    case_delete_flow,
    case_escalated_create_flow,
    case_new_create_flow,
    case_remove_participant_flow,
    case_stable_create_flow,
    case_to_incident_endpoint_escalate_flow,
    case_triage_create_flow,
    case_update_flow,
    get_case_participants_flow,
)
from .models import (
    Case,
    CaseCreate,
    CaseExpandedPagination,
    CasePagination,
    CasePaginationMinimalWithExtras,
    CaseRead,
    CaseUpdate,
)
from .service import create, delete, get, get_participants, update

log = logging.getLogger(__name__)

router = APIRouter()


def get_current_case(db_session: DbSession, request: Request) -> Case:
    """Fetches a case or returns an HTTP 404."""
    case = get(db_session=db_session, case_id=request.path_params["case_id"])
    if not case:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=[{"msg": "The requested case does not exist."}],
        )
    return case


CurrentCase = Annotated[Case, Depends(get_current_case)]


@router.get(
    "/{case_id}",
    response_model=CaseRead,
    summary="Retrieves a single case.",
    dependencies=[Depends(PermissionsDependency([CaseViewPermission]))],
)
def get_case(
    case_id: PrimaryKey,
    db_session: DbSession,
    current_case: CurrentCase,
):
    """Retrieves the details of a single case."""
    return current_case


@router.get(
    "/{case_id}/participants/minimal",
    response_model=list[ParticipantReadMinimal],
    summary="Retrieves a minimal list of case participants.",
    dependencies=[Depends(PermissionsDependency([CaseViewPermission]))],
)
def get_case_participants_minimal(
    case_id: PrimaryKey,
    db_session: DbSession,
):
    """Retrieves the details of a single case."""
    return get_participants(case_id=case_id, db_session=db_session)


@router.get(
    "/{case_id}/participants",
    summary="Retrieves a list of case participants.",
    dependencies=[Depends(PermissionsDependency([CaseViewPermission]))],
)
def get_case_participants(
    case_id: PrimaryKey,
    db_session: DbSession,
    minimal: bool = Query(default=False),
):
    """Retrieves the details of a single case."""
    participants = get_participants(case_id=case_id, db_session=db_session, minimal=minimal)

    if minimal:
        return [ParticipantReadMinimal.from_orm(p) for p in participants]
    else:
        return [ParticipantRead.from_orm(p) for p in participants]


@router.get("", summary="Retrieves a list of cases.")
def get_cases(
    common: CommonParameters,
    include: list[str] = Query([], alias="include[]"),
    expand: bool = Query(default=False),
):
    """Retrieves all cases."""
    pagination = search_filter_sort_paginate(model="Case", **common)

    if expand:
        return json.loads(CaseExpandedPagination(**pagination).json())

    if include:
        # only allow two levels for now
        include_sets = create_pydantic_include(include)

        include_fields = {
            "items": {"__all__": include_sets},
            "itemsPerPage": ...,
            "page": ...,
            "total": ...,
        }
        return json.loads(CaseExpandedPagination(**pagination).json(include=include_fields))
    return json.loads(CasePagination(**pagination).json())


@router.get("/minimal", summary="Retrieves a list of cases with minimal data.")
def get_cases_minimal(
    common: CommonParameters,
):
    """Retrieves all cases with minimal data."""
    pagination = search_filter_sort_paginate(model="Case", **common)

    return json.loads(CasePaginationMinimalWithExtras(**pagination).json())


@router.post("", response_model=CaseRead, summary="Creates a new case.")
def create_case(
    db_session: DbSession,
    organization: OrganizationSlug,
    case_in: CaseCreate,
    current_user: CurrentUser,
    background_tasks: BackgroundTasks,
):
    """Creates a new case."""
    # TODO: (wshel) this conditional always happens in the UI flow since
    # reporter is not available to be set.
    if not case_in.reporter:
        # Ensure the individual exists, create if not
        if case_in.project is None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=[{"msg": "Project must be set to create reporter individual."}],
            )
        # Fetch the full DB project instance
        project = project_service.get_by_name_or_default(
            db_session=db_session, project_in=case_in.project
        )
        individual = get_or_create(
            db_session=db_session,
            email=current_user.email,
            project=project,
        )
        case_in.reporter = ParticipantUpdate(
            individual=IndividualContactRead(id=individual.id, email=individual.email)
        )

    try:
        case = create(db_session=db_session, case_in=case_in, current_user=current_user)
    except ValueError as e:
        log.exception(e)
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, detail=[{"msg": e.args[0]}]
        ) from e

    if case.status == CaseStatus.triage:
        background_tasks.add_task(
            case_triage_create_flow,
            case_id=case.id,
            organization_slug=organization,
        )
    elif case.status == CaseStatus.escalated:
        background_tasks.add_task(
            case_escalated_create_flow,
            case_id=case.id,
            organization_slug=organization,
        )
    elif case.status == CaseStatus.closed:
        background_tasks.add_task(
            case_closed_create_flow,
            case_id=case.id,
            organization_slug=organization,
        )
    elif case.status == CaseStatus.stable:
        background_tasks.add_task(
            case_stable_create_flow,
            case_id=case.id,
            organization_slug=organization,
        )
    else:
        background_tasks.add_task(
            case_new_create_flow,
            case_id=case.id,
            db_session=db_session,
            organization_slug=organization,
        )

    return case


@router.post(
    "/{case_id}/resources/conversation",
    response_model=CaseRead,
    summary="Creates conversation channel for an existing case.",
)
def create_case_channel(
    db_session: DbSession,
    current_case: CurrentCase,
):
    """Creates conversation channel for an existing case."""

    current_case.dedicated_channel = True

    # Add all case participants to the case channel
    case_create_conversation_flow(
        db_session=db_session,
        case=current_case,
        participant_emails=current_case.participant_emails,
        conversation_target=None,
    )

    return current_case


@router.post(
    "/{case_id}/resources",
    response_model=CaseRead,
    summary="Creates resources for an existing case.",
)
def create_case_resources(
    db_session: DbSession,
    case_id: PrimaryKey,
    current_case: CurrentCase,
    background_tasks: BackgroundTasks,
):
    """Creates resources for an existing case."""
    individual_participants, team_participants = get_case_participants_flow(
        case=current_case, db_session=db_session
    )
    background_tasks.add_task(
        case_create_resources_flow,
        db_session=db_session,
        case_id=case_id,
        individual_participants=individual_participants,
        team_participants=team_participants,
    )

    return current_case


@router.put(
    "/{case_id}",
    response_model=CaseRead,
    summary="Updates an existing case.",
    dependencies=[Depends(PermissionsDependency([CaseEditPermission]))],
)
def update_case(
    db_session: DbSession,
    current_case: CurrentCase,
    organization: OrganizationSlug,
    case_id: PrimaryKey,
    case_in: CaseUpdate,
    current_user: CurrentUser,
    background_tasks: BackgroundTasks,
):
    """Updates an existing case."""
    reporter_email = None
    if case_in.reporter:
        # we assign the case to the reporter provided
        reporter_email = case_in.reporter.individual.email
    elif current_user:
        # we fall back to assign the case to the current user
        reporter_email = current_user.email

    assignee_email = None
    if case_in.assignee:
        # we assign the case to the assignee provided
        assignee_email = case_in.assignee.individual.email
    elif current_user:
        # we fall back to assign the case to the current user
        assignee_email = current_user.email

    # we store the previous state of the case in order to be able to detect changes
    previous_case = CaseRead.from_orm(current_case)

    # we update the case
    case = update(
        db_session=db_session, case=current_case, case_in=case_in, current_user=current_user
    )

    # we run the case update flow
    background_tasks.add_task(
        case_update_flow,
        case_id=case_id,
        previous_case=previous_case,
        reporter_email=reporter_email,
        assignee_email=assignee_email,
        organization_slug=organization,
    )

    return case


@router.put(
    "/{case_id}/escalate",
    response_model=IncidentRead,
    summary="Escalates an existing case.",
    dependencies=[Depends(PermissionsDependency([CaseEditPermission]))],
)
def escalate_case(
    db_session: DbSession,
    current_case: CurrentCase,
    organization: OrganizationSlug,
    incident_in: IncidentCreate,
    current_user: CurrentUser,
    background_tasks: BackgroundTasks,
):
    """Escalates an existing case."""
    # use existing reporter or current user if not provided
    if not incident_in.reporter:
        incident_in.reporter = (
            ParticipantUpdate(
                individual=IndividualContactRead(email=current_case.reporter.individual.email)
            )
            if current_case.reporter
            else ParticipantUpdate(individual=IndividualContactRead(email=current_user.email))
        )

    # allow for default values
    if not incident_in.incident_type:
        if current_case.case_type.incident_type:
            incident_in.incident_type = {"name": current_case.case_type.incident_type.name}

    if not incident_in.project:
        if current_case.case_type.incident_type:
            incident_in.project = {"name": current_case.case_type.incident_type.project.name}

    incident = incident_service.create(db_session=db_session, incident_in=incident_in)
    background_tasks.add_task(
        case_to_incident_endpoint_escalate_flow,
        case_id=current_case.id,
        incident_id=incident.id,
        organization_slug=organization,
    )

    return incident


@router.delete(
    "/{case_id}",
    response_model=None,
    summary="Deletes an existing case and its external resources.",
    dependencies=[Depends(PermissionsDependency([CaseEditPermission]))],
)
def delete_case(
    case_id: PrimaryKey,
    db_session: DbSession,
    current_case: CurrentCase,
):
    """Deletes an existing case and its external resources."""
    # we run the case delete flow
    case_delete_flow(case=current_case, db_session=db_session)

    # we delete the internal case
    try:
        delete(db_session=db_session, case_id=case_id)
    except IntegrityError as e:
        log.exception(e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=[
                {
                    "msg": (
                        f"Case {current_case.name} could not be deleted. Make sure the case has no "
                        "relationships to other cases or incidents before deleting it.",
                    )
                }
            ],
        ) from None


@router.post(
    "/{case_id}/join",
    summary="Adds an individual to a case.",
    dependencies=[Depends(PermissionsDependency([CaseJoinPermission]))],
)
def join_case(
    db_session: DbSession,
    organization: OrganizationSlug,
    case_id: PrimaryKey,
    current_case: CurrentCase,
    current_user: CurrentUser,
    background_tasks: BackgroundTasks,
):
    """Adds an individual to a case."""
    background_tasks.add_task(
        case_add_or_reactivate_participant_flow,
        current_user.email,
        case_id=current_case.id,
        organization_slug=organization,
    )


@router.delete(
    "/{case_id}/remove/{email}",
    summary="Removes an individual from a case.",
    dependencies=[Depends(PermissionsDependency([CaseEditPermission]))],
)
def remove_participant_from_case(
    db_session: DbSession,
    organization: OrganizationSlug,
    case_id: PrimaryKey,
    email: str,
    current_case: CurrentCase,
    current_user: CurrentUser,
    background_tasks: BackgroundTasks,
):
    """Removes an individual from a case."""
    background_tasks.add_task(
        case_remove_participant_flow,
        email,
        case_id=current_case.id,
        db_session=db_session,
    )


@router.post(
    "/{case_id}/add/{email}",
    summary="Adds an individual to a case.",
    dependencies=[Depends(PermissionsDependency([CaseEditPermission]))],
)
def add_participant_to_case(
    db_session: DbSession,
    organization: OrganizationSlug,
    case_id: PrimaryKey,
    email: str,
    current_case: CurrentCase,
    current_user: CurrentUser,
    background_tasks: BackgroundTasks,
):
    """Adds an individual to a case."""
    background_tasks.add_task(
        case_add_or_reactivate_participant_flow,
        email,
        case_id=current_case.id,
        organization_slug=organization,
        db_session=db_session,
    )


@router.post(
    "/{case_id}/event",
    summary="Creates a custom event.",
    dependencies=[Depends(PermissionsDependency([CaseEventPermission]))],
)
def create_custom_event(
    db_session: DbSession,
    organization: OrganizationSlug,
    case_id: PrimaryKey,
    current_case: CurrentCase,
    event_in: EventCreateMinimal,
    current_user: CurrentUser,
    background_tasks: BackgroundTasks,
):
    if event_in.details is None:
        event_in.details = {}
    event_in.details.update({"created_by": current_user.email, "added_on": str(datetime.utcnow())})
    """Creates a custom event."""
    background_tasks.add_task(
        event_flows.log_case_event,
        user_email=current_user.email,
        case_id=current_case.id,
        event_in=event_in,
        organization_slug=organization,
    )


@router.patch(
    "/{case_id}/event",
    summary="Updates a custom event.",
    dependencies=[Depends(PermissionsDependency([CaseEventPermission]))],
)
def update_custom_event(
    db_session: DbSession,
    organization: OrganizationSlug,
    case_id: PrimaryKey,
    current_case: CurrentCase,
    event_in: EventUpdate,
    current_user: CurrentUser,
    background_tasks: BackgroundTasks,
):
    if event_in.details:
        event_in.details.update(
            {
                **event_in.details,
                "updated_by": current_user.email,
                "updated_on": str(datetime.utcnow()),
            }
        )
    else:
        event_in.details = {"updated_by": current_user.email, "updated_on": str(datetime.utcnow())}
    """Updates a custom event."""
    background_tasks.add_task(
        event_flows.update_case_event,
        event_in=event_in,
        organization_slug=organization,
    )


@router.post(
    "/{case_id}/exportTimeline",
    summary="Exports timeline events.",
    dependencies=[Depends(PermissionsDependency([CaseEventPermission]))],
)
def export_timeline_event(
    db_session: DbSession,
    organization: OrganizationSlug,
    case_id: PrimaryKey,
    current_case: CurrentCase,
    timeline_filters: dict,
    current_user: CurrentUser,
    background_tasks: BackgroundTasks,
):
    try:
        event_flows.export_case_timeline(
            timeline_filters=timeline_filters,
            case_id=case_id,
            organization_slug=organization,
            db_session=db_session,
        )
    except Exception as e:
        log.exception(e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=[{"msg": (f"{str(e)}.",)}],
        ) from e


@router.delete(
    "/{case_id}/event/{event_uuid}",
    summary="Deletes a custom event.",
    dependencies=[Depends(PermissionsDependency([CaseEventPermission]))],
)
def delete_custom_event(
    db_session: DbSession,
    organization: OrganizationSlug,
    case_id: PrimaryKey,
    current_case: CurrentCase,
    event_uuid: str,
    current_user: CurrentUser,
    background_tasks: BackgroundTasks,
):
    """Deletes a custom event."""
    background_tasks.add_task(
        event_flows.delete_case_event,
        event_uuid=event_uuid,
        organization_slug=organization,
    )
