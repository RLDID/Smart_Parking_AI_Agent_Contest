"""Explicitly installed local relationship administration routes."""
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse

from backend.auth import ApiError
from backend.relationships import Relationships
from contracts.relationships import (CustomerChange, CustomerCreate, ObjectMappingChange,
                                     PersonMappingChange, VehicleChange, VehicleCreate,
                                     VehicleUserChange)

_PAGE = Path(__file__).resolve().parents[1] / "devtools"


def install_relationship_routes(app, authenticate, facility_check, settings):
    router = APIRouter()

    def access(request, facility_id, *, mutation=False):
        session = authenticate(request, ["owner", "test_operator"], mutation=mutation)
        facility_check(facility_id)
        return session

    async def change(request, facility_id, action, body, operation):
        session = access(request, facility_id, mutation=True)
        runtime = app.state.runtime
        async with runtime.lock:
            current = access(request, facility_id, mutation=True)
            if current.username != session.username or current.role != session.role:
                raise ApiError(403, "ACCESS_CHANGED", "관리 권한이 변경되었습니다.")
            if runtime.failure:
                raise ApiError(503, "STATE_UNAVAILABLE", "현재 저장 상태를 확인할 수 없습니다.")
            relationships = Relationships(runtime.store.db)
            return relationships.execute(session.username, request.headers.get("idempotency-key"),
                                         action, body.model_dump(), lambda: operation(relationships, session, runtime))

    @router.get("/api/v1/facilities/{facility_id}/relationships")
    async def relationships(facility_id: str, request: Request):
        session = access(request, facility_id)
        runtime = app.state.runtime
        async with runtime.lock:
            current = access(request, facility_id)
            if current.username != session.username or current.role != session.role:
                raise ApiError(403, "ACCESS_CHANGED", "관리 권한이 변경되었습니다.")
            if runtime.failure:
                raise ApiError(503, "STATE_UNAVAILABLE", "현재 저장 상태를 확인할 수 없습니다.")
            return Relationships(runtime.store.db).snapshot()

    @router.post("/api/v1/facilities/{facility_id}/relationships/customers", status_code=201)
    async def create_customer(facility_id: str, body: CustomerCreate, request: Request):
        return await change(request, facility_id, "customer.create", body,
                            lambda rel, session, runtime: rel.customer_create(session.username, body.display_alias, body.reason))

    @router.patch("/api/v1/facilities/{facility_id}/relationships/customers/{user_id}")
    async def change_customer(facility_id: str, user_id: str, body: CustomerChange, request: Request):
        return await change(request, facility_id, "customer.change:" + user_id, body,
                            lambda rel, session, runtime: rel.customer_change(session.username, user_id, body))

    @router.post("/api/v1/facilities/{facility_id}/relationships/vehicles", status_code=201)
    async def create_vehicle(facility_id: str, body: VehicleCreate, request: Request):
        return await change(request, facility_id, "vehicle.create", body,
                            lambda rel, session, runtime: rel.vehicle_create(session.username, body.display_alias, body.reason))

    @router.patch("/api/v1/facilities/{facility_id}/relationships/vehicles/{vehicle_id}")
    async def change_vehicle(facility_id: str, vehicle_id: str, body: VehicleChange, request: Request):
        return await change(request, facility_id, "vehicle.change:" + vehicle_id, body,
                            lambda rel, session, runtime: rel.vehicle_change(session.username, vehicle_id, body))

    @router.put("/api/v1/facilities/{facility_id}/relationships/vehicles/{vehicle_id}/customer")
    async def change_vehicle_user(facility_id: str, vehicle_id: str, body: VehicleUserChange, request: Request):
        return await change(request, facility_id, "vehicle.user:" + vehicle_id, body,
                            lambda rel, session, runtime: rel.vehicle_user_change(session.username, vehicle_id, body))

    @router.put("/api/v1/facilities/{facility_id}/relationships/vehicle-objects/{object_id}")
    async def change_vehicle_object(facility_id: str, object_id: str, body: ObjectMappingChange, request: Request):
        return await change(request, facility_id, "vehicle.object:" + object_id, body,
                            lambda rel, session, runtime: rel.object_change(session.username, object_id, body, runtime.world))

    @router.put("/api/v1/facilities/{facility_id}/relationships/person-objects/{object_id}")
    async def change_person_object(facility_id: str, object_id: str, body: PersonMappingChange, request: Request):
        return await change(request, facility_id, "person.object:" + object_id, body,
                            lambda rel, session, runtime: rel.person_change(session.username, object_id, body, runtime.world))

    @router.get("/devtools/relationships")
    async def relationship_page(request: Request):
        if not settings.test_control:
            raise ApiError(404, "NOT_FOUND", "시험 화면이 비활성화되어 있습니다.")
        authenticate(request, ["owner", "test_operator"])
        return FileResponse(_PAGE / "relationships.html")

    @router.get("/devtools/relationships.js")
    async def relationship_script(request: Request):
        if not settings.test_control:
            raise ApiError(404, "NOT_FOUND", "시험 화면이 비활성화되어 있습니다.")
        authenticate(request, ["owner", "test_operator"])
        return FileResponse(_PAGE / "relationships.js", media_type="text/javascript")

    app.include_router(router)
