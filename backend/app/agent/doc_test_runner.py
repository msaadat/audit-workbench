"""Resumable local runner for document-test worklists."""

from __future__ import annotations

from .. import doc_tests
from ..workspaces import Workspace, WorkspaceError
from . import store
from .base import BaseRunner, Cancelled, LimitExceeded


class DocTestRunner(BaseRunner):
    stage_titles = {"matching": "Document matching", "summary": "Result roll-up"}

    def execute(self) -> None:
        if not self.run.get("started"):
            self.run["started"] = store.utcnow()
        try:
            test_id = str(self.context.get("test_id") or "")
            if not test_id:
                raise WorkspaceError("A document-test run requires context.test_id.")
            test = doc_tests.load_test(self.ws, test_id)
            self.run["doc_test"] = {"test_id": test_id}
            self._prepare(test)
            self.set_status("executing")
            for item in test.get("items") or []:
                task = self._task(f"doctest:{test_id}:{item['id']}")
                if task is None or task["status"] in {"completed", "skipped"}:
                    continue
                current = next(
                    (value for value in doc_tests.load_test(self.ws, test_id)["items"] if value["id"] == item["id"]),
                    item,
                )
                if current.get("state") in {"confirmed", "exception"}:
                    self.task_status(task, "skipped")
                    continue
                self.checkpoint()
                self.task_status(task, "running")
                try:
                    result = doc_tests.run_item(self.ws, test_id, item["id"], run_id=self.run["id"])
                    task["result_refs"] = [f"doctest:{test_id}:{item['id']}"]
                    self.task_status(task, "completed")
                    self.emit("workspace_changed", {"kind": "doctest", "id": test_id, "item_id": item["id"]})
                    if result.get("state") == "manual_review":
                        self.warn(f"{result.get('label') or item['id']} requires a manual check.")
                except (Cancelled, LimitExceeded):
                    raise
                except Exception as error:
                    self.task_status(task, "failed", str(error))
                    self.warn(f"Could not check {item.get('label') or item['id']}: {error}")
            test = doc_tests.load_test(self.ws, test_id)
            rollup = doc_tests.result_rollup(test)
            test["status"] = "completed" if not (rollup["pending"] or rollup["manual_review"]) else "in_progress"
            doc_tests.save_test(self.ws, test)
            self.run["doc_test"].update({"rollup": rollup, "test_sha1": test["sha1"]})
            self.run["summary_markdown"] = self._summary(test, rollup)
            self.save()
            self.run["finished"] = store.utcnow()
            self.set_status("completed")
            self.emit("summary_ready", {"run_id": self.run["id"]})
        except Cancelled:
            self.run["finished"] = store.utcnow()
            self.set_status("cancelled")
        except LimitExceeded as error:
            self.run["error"] = str(error)
            self.run["finished"] = store.utcnow()
            self.set_status("failed")
        except Exception as error:
            self.run["error"] = str(error)
            self.run["finished"] = store.utcnow()
            self.set_status("failed")

    def _prepare(self, test: dict) -> None:
        if not self.run["plan"]["stages"]:
            for item in test.get("items") or []:
                self.add_task(
                    "matching", f"doctest:{test['id']}:{item['id']}",
                    f"Check {item.get('label') or item['id']}",
                    "Run deterministic comparisons and retain source anchors.",
                )
            self.emit("plan_update", {"stages": self.run["plan"]["stages"]})

    @staticmethod
    def _summary(test: dict, rollup: dict) -> str:
        return (
            f"# Document test — {test['title']}\n\n"
            f"- Items: {rollup['items']}\n"
            f"- Deterministic matches: {rollup['matched']}\n"
            f"- Mismatch or missing: {rollup['mismatched']}\n"
            f"- Confirmed: {rollup['confirmed']}\n"
            f"- Exceptions: {rollup['exceptions']}\n"
            f"- Manual review: {rollup['manual_review']}\n\n"
            "Automated results remain subject to auditor disposition."
        )
