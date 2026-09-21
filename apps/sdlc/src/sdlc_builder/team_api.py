"""Account and project membership endpoints; no email is sent automatically."""

import asyncio
import secrets
from urllib.parse import urlsplit

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .collaboration import SESSION_SECONDS


class Login(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=256)


class Join(Login):
    name: str = Field(min_length=1, max_length=80, pattern=r".*\S.*")
    token: str = Field(min_length=20, max_length=200)


class Invite(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    role: str = Field(default="developer", pattern="^(viewer|developer|maintainer)$")


class Membership(BaseModel):
    role: str = Field(pattern="^(viewer|developer|maintainer|owner)$")


def install_routes(app, team, launch_token: str):
    # Bound password hashing memory across concurrent sign-ins.
    auth_slots = asyncio.Semaphore(2)

    def enabled():
        if not team.enabled:
            raise HTTPException(409, "Enable shared mode to use team accounts")

    async def authenticate(request, operation, *args):
        enabled()
        email = args[0] if args else ""
        team.throttle(request.client.host if request.client else "unknown", email)
        async with auth_slots:
            user = await asyncio.to_thread(operation, *args)
        token = team.session(user["id"])
        response = JSONResponse({"user": user})
        response.set_cookie(
            "sdlc_session",
            token,
            max_age=SESSION_SECONDS,
            httponly=True,
            secure=urlsplit(team.origin).scheme == "https",
            samesite="strict",
        )
        return response

    @app.get("/api/auth/status")
    async def status(request: Request):
        user = (
            team.session_user(request.cookies.get("sdlc_session", ""))
            if team.enabled
            else None
        )
        return {
            "shared": team.enabled,
            "setup_required": team.enabled and team.setup_required(),
            "user": user,
        }

    @app.post("/api/auth/bootstrap")
    async def bootstrap(request: Request, body: Join):
        enabled()
        if not secrets.compare_digest(body.token, launch_token):
            raise HTTPException(401, "The setup token is incorrect")
        return await authenticate(
            request, team.bootstrap, body.email, body.name, body.password
        )

    @app.post("/api/auth/login")
    async def login(request: Request, body: Login):
        return await authenticate(request, team.login, body.email, body.password)

    @app.post("/api/auth/join")
    async def join(request: Request, body: Join):
        # Preserve the email-based throttle key even though the invite method
        # takes its token first.
        def accept(email, name, password):
            return team.accept_invite(body.token, email, name, password)

        return await authenticate(request, accept, body.email, body.name, body.password)

    @app.post("/api/auth/logout")
    async def logout(request: Request):
        team.logout(request.cookies.get("sdlc_session", ""))
        response = JSONResponse({"ok": True})
        response.delete_cookie("sdlc_session")
        return response

    @app.get("/api/projects/hidden")
    async def hidden_projects():
        return team.projects(hidden=True)

    @app.post("/api/projects/{project_id}/show")
    async def show_project(project_id: str):
        team.require(project_id)
        team.store.get("projects", project_id)
        team.preference(project_id, hidden=False)
        return {"ok": True}

    @app.get("/api/projects/{project_id}/members")
    async def members(project_id: str):
        enabled()
        team.require(project_id)
        return {"members": team.members(project_id), "role": team.role(project_id)}

    @app.post("/api/projects/{project_id}/invitations")
    async def invite(project_id: str, body: Invite):
        enabled()
        result = team.invite(project_id, body.email, body.role)
        team.audit(
            project_id,
            "member.invited",
            detail={"email": result["email"], "role": body.role},
        )
        return result

    @app.get("/api/projects/{project_id}/invitations")
    async def invitations(project_id: str):
        team.require(project_id, "owner")
        with team.store.connect() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT id,email,role,expires FROM invitations WHERE project_id=? AND used_at IS NULL ORDER BY expires DESC",
                    (project_id,),
                )
            ]

    @app.delete("/api/projects/{project_id}/invitations/{invitation_id}")
    async def revoke_invitation(project_id: str, invitation_id: str):
        team.require(project_id, "owner")
        with team.store.connect() as db:
            db.execute(
                "DELETE FROM invitations WHERE id=? AND project_id=? AND used_at IS NULL",
                (invitation_id, project_id),
            )
        team.audit(
            project_id, "invitation.revoked", detail={"invitation_id": invitation_id}
        )
        return {"ok": True}

    @app.put("/api/projects/{project_id}/members/{user_id}")
    async def change_member(project_id: str, user_id: str, body: Membership):
        team.change_member(project_id, user_id, body.role)
        team.audit(
            project_id,
            "member.role_changed",
            detail={"user_id": user_id, "role": body.role},
        )
        return {"ok": True}

    @app.delete("/api/projects/{project_id}/members/{user_id}")
    async def remove_member(project_id: str, user_id: str):
        team.change_member(project_id, user_id, None)
        team.audit(project_id, "member.removed", detail={"user_id": user_id})
        return {"ok": True}

    @app.get("/api/projects/{project_id}/activity")
    async def project_activity(project_id: str):
        team.require(project_id)
        return team.events(project_id)

    @app.get("/api/tasks/{task_id}/activity")
    async def task_activity(task_id: str):
        task = team.store.get("tasks", task_id)
        team.require(task["project_id"])
        return team.events(task["project_id"], task_id)


class SharedWebsocketBoundary:
    """The embedded debugger has no project-aware websocket authorization."""

    def __init__(self, app, enabled: bool):
        self.app, self.enabled = app, enabled

    async def __call__(self, scope, receive, send):
        if self.enabled and scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        await self.app(scope, receive, send)
