import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import pytest
from bson import BSON
from conftest import committed_repo, settings_for
from langgraph.checkpoint.mongodb import MongoDBSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from pymongo import MongoClient
from test_artifact_planner import fixture_plan

from voxops.core.errors import AppError
from voxops.domain.artifact_models import ArtifactRun, RepositoryTarget, TaskResult
from voxops.repository.worktree_manager import WorktreeManager
from voxops.storage.artifact_store import ArtifactStore
from voxops.storage.mongodb import database_for
from voxops.storage.scan_store import ScanStore


# Exercise storage through its public operations with a real domain run and scan.
# Recreating each store must recover its data without any local JSON or blob directory.
def storage_roundtrip(database, tmp_path):
    root, analysis, plan = fixture_plan(tmp_path, "fastapi_only", {"fastapi": "container"})
    store = ArtifactStore(database)
    scans = ScanStore(database)
    scans.save(analysis)
    run = ArtifactRun(
        run_id=uuid4().hex,
        repo_fingerprint=plan.repo_fingerprint,
        scan_id=plan.scan_id,
        source_input_fingerprint=plan.source_input_fingerprint,
        source_fingerprint="source",
        source_head_sha=None,
        plan=plan,
        target=RepositoryTarget(owner="example", name="fixture"),
        branch="voxops/test",
    )
    store.save_run(run)
    store.bind_source(run.run_id, root)
    store.save_plan(plan.plan_id, {"plan": plan.model_dump(mode="json"), "source": str(root)})
    key = store.stage(b"FROM python:3.12-slim\n")
    tasks = [TaskResult(task_id=f"task-{n}", status="skipped") for n in range(2)]
    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(lambda task: store.save_task(run.run_id, task), tasks))
    store.save_run(run)
    reopened = ArtifactStore(database)
    assert reopened.load_run(run.run_id).tasks == {t.task_id: t for t in tasks}
    assert "tasks" not in database.artifact_runs.find_one({"_id": run.run_id})
    assert reopened.read_blob(key) == b"FROM python:3.12-slim\n"
    assert reopened.source(run.run_id) == root
    assert reopened.load_plan(plan.plan_id)["plan"] == plan.model_dump(mode="json")
    assert ScanStore(database).load(analysis.scan.id) == analysis
    settings = settings_for(tmp_path)
    manager = WorktreeManager(settings, scans)
    source = committed_repo(tmp_path)
    record = manager.create(str(source))
    try:
        assert WorktreeManager(settings, ScanStore(database)).inspect(record.id).id == record.id
    finally:
        manager.remove(record.id)
    assert not list(tmp_path.rglob("artifact-*.json"))
    assert not list(tmp_path.rglob("scan-*.json"))
    database.artifact_blobs.update_one({"_id": key}, {"$set": {"content": b"corrupt"}})
    with pytest.raises(AppError, match="unavailable"):
        reopened.read_blob(key)
    assert all(
        b"private-test-secret" not in BSON.encode(doc)
        for name in database.list_collection_names()
        for doc in database[name].find()
    )


# Check canonical records and independent task writes using the injected database.
# The same contract is also exercised against MongoDB by the opt-in integration below.
def test_mongo_canonical_roundtrip(tmp_path):
    storage_roundtrip(database_for(settings_for(tmp_path)), tmp_path)


# Pause a tiny native graph using only a safe form reference.
# Its state must survive constructing a new official MongoDB checkpointer.
def checkpoint_pause(state):
    reply = interrupt({"kind": "environment", "schema_digest": "a" * 64})
    return {"acknowledged": bool(reply["accepted"])}


@pytest.mark.skipif(not os.environ.get("VOXOPS_TEST_MONGO_URI"), reason="real MongoDB opt-in")
# Use the configured disposable MongoDB to prove records and native checkpoints survive reload.
# Drop only the unique database created by this test, never an operator database.
def test_real_mongo_reload_and_native_interrupt(tmp_path):
    uri = os.environ["VOXOPS_TEST_MONGO_URI"]
    name = "voxops_test_" + uuid4().hex
    client = MongoClient(uri, serverSelectionTimeoutMS=5000)
    try:
        database = client[name]
        assert database.command("ping")["ok"]
        storage_roundtrip(database, tmp_path)
        builder = StateGraph(dict)
        builder.add_node("form", checkpoint_pause)
        builder.add_edge(START, "form")
        builder.add_edge("form", END)
        config = {"configurable": {"thread_id": uuid4().hex}}
        graph = builder.compile(checkpointer=MongoDBSaver(client, db_name=name))
        first = asyncio.run(graph.ainvoke({}, config))
        assert first["__interrupt__"][0].value["kind"] == "environment"
        with MongoClient(uri) as restarted:
            graph = builder.compile(checkpointer=MongoDBSaver(restarted, db_name=name))
            resumed = asyncio.run(graph.ainvoke(Command(resume={"accepted": True}), config))
            assert resumed["acknowledged"]
            assert restarted[name].artifact_runs.count_documents({}) == 1
        assert not (Path.cwd() / ".voxops" / name).exists()
    finally:
        client.drop_database(name)
        client.close()
