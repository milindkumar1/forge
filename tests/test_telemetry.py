import unittest
from unittest.mock import patch

from forge.config import LocalConfig
from forge.telemetry import NvidiaSmiSampler, metrics_url, parse_nvidia_smi_row, parse_server_metrics


def local_config(base_url="http://127.0.0.1:8000/v1"):
    return LocalConfig("fixture", base_url, "rev", "tok", "Apache-2.0", "bfloat16", "vllm")


class TelemetryParsingTests(unittest.TestCase):
    def test_metrics_url_targets_loopback_server_root(self):
        self.assertEqual(metrics_url(local_config()), "http://127.0.0.1:8000/metrics")
        self.assertEqual(metrics_url(local_config("http://[::1]:9000/v1")), "http://[::1]:9000/metrics")

    def test_prometheus_parser_keeps_selected_values_and_labels(self):
        parsed = parse_server_metrics("""# TYPE vllm:num_requests_running gauge
vllm:num_requests_running{model_name="fixture",engine="0"} 2
vllm:kv_cache_usage_perc{model_name="fixture"} 0.25
vllm:time_to_first_token_seconds_bucket{le="0.5",model_name="fixture"} 7
vllm:time_to_first_token_seconds_bucket{le="+Inf",model_name="fixture"} 7
vllm:time_to_first_token_seconds_count{model_name="fixture"} 7
vllm:private_metric{model_name="fixture"} 11
vllm:num_requests_waiting NaN
""")
        self.assertEqual(parsed["vllm:num_requests_running"][0]["value"], 2)
        self.assertEqual(parsed["vllm:num_requests_running"][0]["labels"], 'model_name="fixture",engine="0"')
        self.assertEqual(parsed["vllm:time_to_first_token_seconds_bucket"][0]["value"], 7)
        self.assertEqual(parsed["vllm:time_to_first_token_seconds_bucket"][1]["labels"],
                         'le="+Inf",model_name="fixture"')
        self.assertNotIn("vllm:private_metric", parsed)
        self.assertNotIn("vllm:num_requests_waiting", parsed)

    def test_gpu_csv_parser_handles_units_and_unavailable_fields(self):
        parsed = parse_nvidia_smi_row(
            '0, 2026/10/26 02:00:00.000, 75 %, N/A, 900 MiB, 16303 MiB, 120.5 W')
        self.assertEqual(parsed["device_index"], 0)
        self.assertEqual(parsed["gpu_utilization_percent"], 75)
        self.assertIsNone(parsed["memory_utilization_percent"])
        self.assertEqual(parsed["memory_used_mib"], 900)
        self.assertEqual(parsed["power_watts"], 120.5)
        self.assertIsNone(parse_nvidia_smi_row("not,csv"))


class TelemetryLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_nvidia_smi_is_reported_without_failing_sweep(self):
        sampler = NvidiaSmiSampler()
        with patch("forge.telemetry.asyncio.create_subprocess_exec", side_effect=FileNotFoundError):
            await sampler.start()
        result = await sampler.close()
        self.assertFalse(result["available"])
        self.assertEqual(result["reason"], "nvidia_smi_unavailable")
        self.assertEqual(result["samples"], [])


if __name__ == "__main__":
    unittest.main()
