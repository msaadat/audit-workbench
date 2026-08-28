"""Administrative CLI for the Audit Workbench control plane.

Accounts are admin-provisioned, so the first administrator has to be created
outside the request path — there is no self-service registration route to reach
for.  Run from the repository root:

    uv run --no-project python backend/manage.py bootstrap-admin --email a@b.com
    uv run --no-project python backend/manage.py list-users
    uv run --no-project python backend/manage.py create-user --email c@d.com
    uv run --no-project python backend/manage.py set-password --email a@b.com
    uv run --no-project python backend/manage.py disable-user --email c@d.com
    uv run --no-project python backend/manage.py reconcile

``reconcile`` is the same pass the app runs at startup: it registers any
workspace folder that is sitting in a user's home without a registry row, and
stamps identity into a manifest that predates it.  It is what makes migrating
existing workspaces a matter of moving folders.
"""

from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app import accounts, config, db, registry  # noqa: E402


def _password(supplied: str | None) -> str:
    if supplied:
        return supplied
    first = getpass.getpass("Password: ")
    if first != getpass.getpass("Confirm password: "):
        raise SystemExit("The passwords did not match.")
    return first


def _resolve(email: str) -> dict:
    user = accounts.find_by_email(email)
    if user is None:
        raise SystemExit(f"No account for '{email}'.")
    return user


def bootstrap_admin(args) -> None:
    if args.adopt_local:
        user = accounts.adopt_local_account(
            args.email, _password(args.password), display_name=args.name or "",
        )
        accounts.record_auth_event("admin.adopt_local", user_id=user["id"], email=user["email"])
        owned = len(registry.list_for_owner(user["id"]))
        print(f"The local account is now administrator {user['email']} ({user['id']}).")
        print(f"Its {owned} workspace(s) stayed where they are: {registry.user_home(user['id'])}")
        return
    existing = [user for user in accounts.list_users() if user["is_admin"]
                and user["id"] != accounts.LOCAL_USER_ID]
    if existing and not args.force:
        raise SystemExit(
            f"An administrator already exists ({existing[0]['email']}). "
            "Pass --force to add another."
        )
    user = accounts.create_user(
        args.email, _password(args.password),
        display_name=args.name or "", is_admin=True,
    )
    registry.user_workspaces_dir(user["id"]).mkdir(parents=True, exist_ok=True)
    accounts.record_auth_event("admin.bootstrap", user_id=user["id"], email=user["email"])
    print(f"Created administrator {user['email']} ({user['id']}).")
    print(f"Home: {registry.user_home(user['id'])}")
    print("This account owns no workspaces. If this installation already has "
          "engagements, they belong to the local account — re-run with "
          "--adopt-local instead to promote it in place.")


def create_user(args) -> None:
    user = accounts.create_user(
        args.email, _password(args.password),
        display_name=args.name or "", is_admin=args.admin,
    )
    registry.user_workspaces_dir(user["id"]).mkdir(parents=True, exist_ok=True)
    print(f"Created {user['email']} ({user['id']}).")
    print(f"Home: {registry.user_home(user['id'])}")


def set_password(args) -> None:
    user = _resolve(args.email)
    accounts.set_password(user["id"], _password(args.password))
    print(f"Password updated for {user['email']}.")


def disable_user(args) -> None:
    user = _resolve(args.email)
    accounts.set_status(user["id"], "disabled")
    print(f"Disabled {user['email']}; their sessions were revoked.")


def enable_user(args) -> None:
    user = _resolve(args.email)
    accounts.set_status(user["id"], "active")
    print(f"Enabled {user['email']}.")


def list_users(_args) -> None:
    users = accounts.list_users()
    if not users:
        print("No accounts yet.")
        return
    width = max(len(user["email"]) for user in users)
    for user in users:
        role = "admin" if user["is_admin"] else "user"
        owned = len(registry.list_for_owner(user["id"]))
        print(f"{user['email']:<{width}}  {user['id']:<28} {role:<6} "
              f"{user['status']:<8} {owned} workspace(s)")


def reconcile(_args) -> None:
    summary = registry.reconcile()
    print(f"Scanned {summary['scanned']} workspace folder(s): "
          f"{summary['stamped']} stamped with identity, "
          f"{summary['registered']} newly registered.")


def show_paths(_args) -> None:
    from app import auth

    print(f"Data root:   {config.data_root()}")
    print(f"Database:    {db.database_path()}")
    print(f"Users:       {registry.users_dir()}")
    print(f"Auth mode:   {auth.auth_mode()}")
    if auth.single_user_mode():
        home = registry.user_workspaces_dir(auth.local_principal().user_id)
        print(f"Workspaces:  {home}")
        print("             (this is where existing workspaces must be moved)")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="manage.py", description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add(name, handler, *, email=False, password=False, name_opt=False):
        sub = subparsers.add_parser(name)
        if email:
            sub.add_argument("--email", required=True)
        if password:
            sub.add_argument("--password", help="Prompted for when omitted.")
        if name_opt:
            sub.add_argument("--name", help="Display name.")
        sub.set_defaults(handler=handler)
        return sub

    boot = add("bootstrap-admin", bootstrap_admin, email=True, password=True, name_opt=True)
    boot.add_argument("--force", action="store_true",
                      help="Create another administrator even if one exists.")
    boot.add_argument("--adopt-local", action="store_true",
                      help="Promote the existing local account instead of creating "
                           "a new one, so its workspaces do not have to move.")
    created = add("create-user", create_user, email=True, password=True, name_opt=True)
    created.add_argument("--admin", action="store_true")
    add("set-password", set_password, email=True, password=True)
    add("disable-user", disable_user, email=True)
    add("enable-user", enable_user, email=True)
    add("list-users", list_users)
    add("reconcile", reconcile)
    add("paths", show_paths)

    args = parser.parse_args(argv)
    try:
        args.handler(args)
    except (accounts.AccountError, registry.RegistryError) as error:
        raise SystemExit(str(error))


if __name__ == "__main__":
    main()
