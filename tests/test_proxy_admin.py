from app.proxy_admin import allocate_proxy_names


def test_allocate_proxy_names_fills_gaps_and_avoids_existing_names():
    names = allocate_proxy_names(
        "代理",
        4,
        {"代理1-jp.Tokyo", "代理2-US.LosAngeles", "代理4-none", "代理13", "其他1", "代理abc"},
        ["jp.Osaka", "US.NewYork", "none", "HK"],
    )

    assert names == ["代理3-jp.Osaka", "代理5-US.NewYork", "代理6-none", "代理7-HK"]


def test_allocate_proxy_names_uses_custom_prefix_only():
    names = allocate_proxy_names("测试", 3, {"代理1", "测试1-US", "测试3-none"})

    assert names == ["测试2-none", "测试4-none", "测试5-none"]
