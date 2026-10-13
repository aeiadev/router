"""Hermetic fake connector with server-bound worker and observer IPC channels.

The observer runs in a fresh spawn process with only its observer connection and
test-control connection. The service owns the backing path and authorizes by
the receiving endpoint before it examines a request's claimed credentials.
"""

import hashlib
import json
import multiprocessing
from multiprocessing.connection import wait


_TIMEOUT = 5


def _service_main(path, worker, observer):
    state = {"value": "before", "revision_id": "rev_1"}
    path.write_text(json.dumps(state), encoding="utf-8")
    mutation_count = 0
    available = True
    channels = {worker: "worker", observer: "observer"}
    while channels:
        for channel in wait(list(channels), timeout=0.2):
            try:
                request = channel.recv()
            except EOFError:
                del channels[channel]
                channel.close()
                continue
            role = channels[channel]
            op = request.get("op") if type(request) is dict else None
            if op == "write":
                if role != "worker":
                    response = {"error": "denied"}
                elif (type(request.get("value")) is not str
                      or type(request.get("revision_id")) is not str):
                    response = {"error": "invalid"}
                else:
                    state = {"value": request["value"], "revision_id": request["revision_id"]}
                    path.write_text(json.dumps(state), encoding="utf-8")
                    mutation_count += 1
                    response = {"ok": True}
            elif op == "read":
                if role != "observer":
                    response = {"schema_version": 1, "read_status": "denied"}
                elif not available:
                    response = {"schema_version": 1, "read_status": "unavailable"}
                else:
                    readback = json.loads(path.read_text(encoding="utf-8"))
                    response = {"schema_version": 1, "read_status": "ok",
                                "task_id": request.get("task_id"),
                                "target_id": request.get("target_id"),
                                "revision_id": readback["revision_id"],
                                "requirements": {"r1": hashlib.sha256(
                                    readback["value"].encode("utf-8")).hexdigest()},
                                "evidence_id": "readback_1", "verifier_id": "observer_1"}
            elif role == "worker" and op == "stats":
                response = {"mutation_count": mutation_count}
            elif role == "worker" and op == "availability":
                available = request.get("available") is True
                response = {"ok": True}
            elif role == "worker" and op == "stop":
                channel.send({"ok": True})
                return
            else:
                response = {"error": "denied"}
            channel.send(response)


def _observer_main(observer, control):
    # Spawn supplies no parent globals, service instance, worker endpoint, or path.
    while True:
        try:
            request = control.recv()
        except EOFError:
            break
        if request is None:
            break
        observer.send(request)
        if not observer.poll(_TIMEOUT):
            control.send({"error": "timeout"})
            break
        control.send(observer.recv())
    observer.close()
    control.close()


def _exchange(channel, request):
    channel.send(request)
    if not channel.poll(_TIMEOUT):
        raise TimeoutError("fake connector response timed out")
    return channel.recv()


class _Worker:
    def __init__(self, channel):
        self._channel = channel

    def write(self, value, revision_id):
        response = _exchange(self._channel, {"op": "write", "value": value,
                                             "revision_id": revision_id})
        if response != {"ok": True}:
            raise PermissionError("write denied")

    def read(self, task_id, target_id):
        return _exchange(self._channel, {"op": "read", "task_id": task_id,
                                         "target_id": target_id})


class _Observer:
    def __init__(self, control):
        self._control = control

    def request(self, request):
        return _exchange(self._control, request)

    def write(self, value, revision_id):
        response = self.request({"op": "write", "value": value,
                                 "revision_id": revision_id})
        if response != {"ok": True}:
            raise PermissionError("write denied")

    def read(self, task_id, target_id):
        return self.request({"op": "read", "task_id": task_id, "target_id": target_id})


class FakeConnector:
    def __init__(self, path):
        self.path = path
        context = multiprocessing.get_context("spawn")
        worker, worker_server = context.Pipe()
        observer, observer_server = context.Pipe()
        control, actor_control = context.Pipe()
        self._service = context.Process(target=_service_main, args=(path, worker_server, observer_server))
        self._service.start()
        worker_server.close()
        observer_server.close()
        self._actor = context.Process(target=_observer_main, args=(observer, actor_control))
        self._actor.start()
        observer.close()
        actor_control.close()
        self.worker = _Worker(worker)
        self.observer = _Observer(control)
        # The first round trip also establishes that the service is ready.
        _exchange(worker, {"op": "stats"})

    @property
    def mutation_count(self):
        return _exchange(self.worker._channel, {"op": "stats"})["mutation_count"]

    @property
    def available(self):
        raise AttributeError("write-only fixture control")

    @available.setter
    def available(self, value):
        _exchange(self.worker._channel, {"op": "availability", "available": value})

    def close(self):
        try:
            self.observer._control.send(None)
            _exchange(self.worker._channel, {"op": "stop"})
        finally:
            self.observer._control.close()
            self.worker._channel.close()
            for process in (self._actor, self._service):
                process.join(timeout=_TIMEOUT)
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=_TIMEOUT)
