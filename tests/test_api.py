from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from smart_factory_scada.api import create_app
from smart_factory_scada.config import Settings


class ApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.settings = Settings(
            use_kafka=False,
            simulation_tick_seconds=0.05,
            history_limit=10,
            event_limit=10,
            alarm_limit=10,
            uploaded_data_dir=self.temp_dir.name,
        )
        self.client_context = TestClient(create_app(self.settings))
        self.client = self.client_context.__enter__()

    def tearDown(self) -> None:
        self.client_context.__exit__(None, None, None)
        self.temp_dir.cleanup()

    def test_dashboard_renders_initial_snapshot(self) -> None:
        response = self.client.get("/api/dashboard")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["line_name"], self.settings.line_name)
        self.assertEqual(len(payload["machines"]), 5)
        self.assertIn("metrics", payload)
        self.assertIn("Bosch", payload["data_source"])
        self.assertGreater(payload["machines"][0]["signal_rms"], 0.0)

    def test_line_stop_updates_dashboard(self) -> None:
        response = self.client.post("/api/control/line", json={"action": "stop"})
        self.assertEqual(response.status_code, 200)
        time.sleep(0.1)
        payload = self.client.get("/api/dashboard").json()
        self.assertEqual(payload["metrics"]["line_state"], "STOPPED")

    def test_health_endpoint_reports_real_data_source(self) -> None:
        payload = self.client.get("/api/health").json()
        self.assertIn("Bosch", payload["data_source"])

    def test_index_disables_caching_and_versions_assets(self) -> None:
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("no-store", response.headers["cache-control"])
        self.assertIn("/static/app.js?v=", response.text)
        self.assertIn("/static/styles.css?v=", response.text)

    def test_stream_channel_catalog_lists_sse_and_websocket(self) -> None:
        payload = self.client.get("/api/stream/channels").json()
        channel_ids = {channel["channel_id"] for channel in payload}
        self.assertIn("server_sent_events", channel_ids)
        self.assertIn("websocket_live", channel_ids)

    def test_websocket_stream_sends_snapshot(self) -> None:
        with self.client.websocket_connect("/ws/events") as websocket:
            payload = websocket.receive_json()
        self.assertIn("metrics", payload)
        self.assertEqual(payload["data_source"], self.settings.data_source_name)

    def test_scenario_endpoint_faults_robot_cell(self) -> None:
        response = self.client.post("/api/scenarios/conveyor-jam", json={"duration_seconds": 10})
        self.assertEqual(response.status_code, 200)
        time.sleep(0.1)
        payload = self.client.get("/api/dashboard").json()
        assembly = next(machine for machine in payload["machines"] if machine["machine_id"] == "ASM-03")
        self.assertEqual(assembly["state"], "fault")

    def test_clear_fault_removes_alarm(self) -> None:
        self.client.post("/api/control/machines/ASM-03", json={"action": "fault", "duration_seconds": 10, "code": "TEST_FAULT"})
        time.sleep(0.05)
        payload = self.client.get("/api/dashboard").json()
        self.assertTrue(any(alarm["machine_id"] == "ASM-03" for alarm in payload["alarms"]))

        response = self.client.post("/api/control/machines/ASM-03", json={"action": "clear_fault"})
        self.assertEqual(response.status_code, 200)
        time.sleep(0.05)
        payload = self.client.get("/api/dashboard").json()
        self.assertFalse(any(alarm["machine_id"] == "ASM-03" for alarm in payload["alarms"]))

    def test_upload_trace_assigns_csv_replay_to_machine(self) -> None:
        csv_payload = b"axis_x,axis_y,axis_z\n0.1,0.2,0.3\n0.4,0.5,0.6\n0.7,0.6,0.5\n0.2,0.1,0.4\n"
        response = self.client.put(
            "/api/data/upload/CUT-02/normal",
            params={"file_name": "cutter_trace.csv", "sample_rate_hz": 1200},
            content=csv_payload,
            headers={"Content-Type": "text/csv"},
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["binding"]["machine_id"], "CUT-02")
        self.assertEqual(body["binding"]["source_format"], "csv")
        self.assertIn("Operator Uploads", body["data_source"])

        dashboard = self.client.get("/api/dashboard").json()
        machine = next(item for item in dashboard["machines"] if item["machine_id"] == "CUT-02")
        self.assertTrue(machine["trace_id"].startswith("upload_cut_02_normal"))
        self.assertEqual(len(dashboard["trace_uploads"]), 1)

        uploads = self.client.get("/api/data/uploads").json()
        self.assertEqual(len(uploads["trace_uploads"]), 1)
        self.assertTrue(uploads["trace_uploads"][0]["file_name"].endswith("_cutter_trace.csv"))
        self.assertIn("cutter_trace.csv", uploads["data_source_detail"])


if __name__ == "__main__":
    unittest.main()
