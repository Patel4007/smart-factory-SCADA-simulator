from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from smart_factory_scada.config import Settings
from smart_factory_scada.simulation import SmartFactoryEngine


class SimulationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.settings = Settings(use_kafka=False, simulation_tick_seconds=1.0, uploaded_data_dir=self.temp_dir.name)
        self.engine = SmartFactoryEngine(self.settings)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_step_emits_machine_and_kpi_events(self) -> None:
        events = self.engine.step()
        machine_events = [payload for topic, payload in events if topic == self.settings.kafka_machine_topic]
        kpi_events = [payload for topic, payload in events if topic == self.settings.kafka_kpi_topic]
        self.assertEqual(len(machine_events), 5)
        self.assertEqual(len(kpi_events), 1)
        self.assertGreaterEqual(machine_events[-1]["throughput_uph"], 0.0)
        self.assertIn("line_state", kpi_events[0])
        self.assertGreater(machine_events[0]["signal_rms"], 0.0)
        self.assertIn("trace_id", machine_events[0])

    def test_conveyor_jam_faults_assembly_station(self) -> None:
        self.engine.inject_scenario("conveyor-jam", duration_seconds=20)
        self.engine.step()
        assembly = next(machine for machine in self.engine.machine_snapshots() if machine["machine_id"] == "ASM-03")
        self.assertEqual(assembly["state"], "fault")
        self.assertEqual(assembly["alarm_code"], "JAM")

    def test_engine_exposes_real_data_source(self) -> None:
        self.assertIn("Bosch", self.engine.data_source)
        self.assertIn("Bosch", self.engine.data_source_detail)

    def test_upload_trace_updates_machine_assignment(self) -> None:
        result = self.engine.upload_trace(
            "QCI-04",
            "fault",
            file_name="qc_drift.csv",
            payload=b"0.1,0.2,0.3\n0.4,0.4,0.5\n0.7,0.8,0.6\n",
            sample_rate_hz=800,
        )
        self.assertIn("qc_drift.csv", result["detail"])
        bindings = self.engine.uploaded_trace_catalog()
        self.assertEqual(len(bindings), 1)
        self.assertEqual(bindings[0]["machine_id"], "QCI-04")
        self.assertEqual(bindings[0]["role"], "fault")
        self.assertEqual(bindings[0]["source_format"], "csv")
        self.assertIn("Operator Uploads", self.engine.data_source)


if __name__ == "__main__":
    unittest.main()
