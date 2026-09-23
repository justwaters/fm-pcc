import sys, asyncio, unittest.mock as mock
import os as _os
sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "..", "..", "src"))
import fm_pcc.app as m

async def main():
    app = m.ChatApp()
    async with app.run_test() as pilot:
        def sync_call_from_thread(fn, *a, **kw):
            return fn(*a, **kw)

        captured_models = []
        def fake_plan_next_step(task, files, folders, cwd, history, backend, model):
            captured_models.append(model)
            return None  # done immediately

        with mock.patch.object(app, "call_from_thread", side_effect=sync_call_from_thread), \
             mock.patch.object(m, "notify"), \
             mock.patch.object(m, "plan_next_step", side_effect=fake_plan_next_step):
            app._run_task.__wrapped__(app, "do something")
        assert captured_models == ["cloud-pro"], captured_models
        print("task uses planning role OK")

        captured = []
        def fake_decompose(question, backend, model):
            captured.append(model)
            return {"answer": "done", "model_used": model}

        with mock.patch.object(app, "call_from_thread", side_effect=sync_call_from_thread), \
             mock.patch.object(m, "notify"), \
             mock.patch.object(m, "decompose_question", side_effect=fake_decompose):
            app._run_ask.__wrapped__(app, "why?")
        assert captured == ["cloud-pro"], captured
        print("ask uses planning role OK")

asyncio.run(main())
