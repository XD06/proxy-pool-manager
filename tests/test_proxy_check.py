from app.proxy_check import normalize_proxycheck_result


def test_normalize_proxycheck_result_keeps_frontend_shape():
    result = normalize_proxycheck_result(
        {
            "proxy_url": "http://127.0.0.1:8001",
            "score": 90,
            "grade": "A",
            "summary": "ok",
            "exit_ip": "203.0.113.10",
            "country": "Japan",
            "country_code": "JP",
            "base_latency_ms": 120,
            "passed_count": 2,
            "warn_count": 1,
            "failed_count": 0,
            "challenge_count": 0,
            "items": [
                {
                    "target": "openai",
                    "status": "pass",
                    "http_status": 401,
                    "latency_ms": 234,
                    "message": "reachable",
                    "cf_ray": "abc",
                }
            ],
        },
        proxy_id=501,
    )

    assert result["id"] == 501
    assert result["exit_ip"] == "203.0.113.10"
    assert result["country"] == "Japan"
    assert result["country_code"] == "JP"
    assert result["score"] == 90
    assert result["grade"] == "A"
    assert result["items"][0]["target"] == "openai"
    assert result["items"][0]["status"] == "pass"
