import sys, asyncio, unittest.mock as mock
import os as _os
sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "..", "..", "src"))
import fm_pcc.app as m

async def main():
    app = m.ChatApp()
    async with app.run_test() as pilot:
        # Roles are honored whatever they're set to (the default is on-device).
        app.subagent_roles["planning"] = "cloud-pro"
        def sync_call_from_thread(fn, *a, **kw):
            return fn(*a, **kw)

        captured_models = []
        def fake_plan_with_model(request, task, files, folders, backend, model, cwd=None):
            captured_models.append(model)
            return [], model  # nothing planned

        # "do something" has no shape the deterministic parser recognizes,
        # so it has to go to the planning model.
        with mock.patch.object(app, "call_from_thread", side_effect=sync_call_from_thread), \
             mock.patch.object(m, "notify"), \
             mock.patch.object(m, "plan_with_model", side_effect=fake_plan_with_model):
            app._run_task.__wrapped__(app, "do something")
        assert captured_models == ["cloud-pro"], captured_models
        print("task uses planning role OK")

        captured = []
        def fake_decompose(question, backend, model, context=""):
            captured.append(model)
            return {"answer": "done", "model_used": model}

        with mock.patch.object(app, "call_from_thread", side_effect=sync_call_from_thread), \
             mock.patch.object(m, "notify"), \
             mock.patch.object(m, "decompose_question", side_effect=fake_decompose):
            app._run_ask.__wrapped__(app, "why?")
        assert captured == ["cloud-pro"], captured
        print("ask uses planning role OK")

asyncio.run(main())
