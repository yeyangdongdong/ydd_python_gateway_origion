# 寮€鍙戜笌鎵╁睍鎸囧崡

闈㈠悜瑕佹敼浠ｇ爜鐨勪汉锛氭€庝箞璺戣捣鏉ャ€佷唬鐮佷粠鍝璧枫€佹祴璇曟€庝箞缁勭粐銆佸父瑙佹敼鍔ㄦ€庝箞鍋氥€?
## 1. 鐜

```powershell
cd D:\Document\gateway\python_issue\ydd_python_gateway_origion
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
```

鏈満宸叉湁涓€涓彲澶嶇敤鐨勮櫄鎷熺幆澧冿細`D:\Document\gateway\python_issue\ydd-python-gateway-venv`
锛圥ython 3.14.0锛屽凡瑁呭ソ鍏ㄩ儴渚濊禆锛夛紝鐩存帴鐢ㄥ畠鍗冲彲锛屼笉蹇呭啀寤?`.venv`銆?
璺戣捣鏉ワ細

```powershell
& "D:\Document\gateway\python_issue\ydd-python-gateway-venv\Scripts\python.exe" run.py
```

璺戞祴璇曪細

```powershell
& "D:\Document\gateway\python_issue\ydd-python-gateway-venv\Scripts\python.exe" -m unittest discover -s tests -v
```

鍙窇鍗曚釜鐢ㄤ緥锛?
```powershell
& "...\python.exe" -m unittest tests.test_gateway.GatewayTests.test_long_history_is_trimmed_to_budget -v
```

> 娉ㄦ剰锛氫竴涓暟鎹簱鍙兘鐢变竴涓繘绋嬫湇鍔°€備笉瑕佷竴杈硅窇 `run.py` 涓€杈圭敤鍚屼竴浠?`data/gateway.sqlite3` 璺戠浜屼釜瀹炰緥銆?> 娴嬭瘯鐢ㄧ殑鏄复鏃剁洰褰曢噷鐨勭嫭绔嬫暟鎹簱锛屾墍浠?璺戞祴璇?鍜?璺戞湇鍔?鍙互骞跺瓨銆?
## 2. 浠ｇ爜鍦板浘涓庨槄璇婚『搴?
寤鸿鎸夎繖涓『搴忚锛屾瘡涓€姝ラ兘鍙緷璧栧墠闈㈠凡缁忕湅杩囩殑鍐呭锛?
| 椤哄簭 | 鏂囦欢 | 琛屾暟 | 璇诲畠鏄负浜嗙煡閬?|
| --- | --- | --- | --- |
| 1 | `run.py` | 37 | 鍏ュ彛銆佺洃鍚湴鍧€銆佹棩蹇楀垵濮嬪寲銆佸澶栫洃鍚繚鎶?|
| 2 | `gateway/app.py` | 719 | 璺敱銆侀壌鏉冿紙token/浼氳瘽/CSRF锛夈€佹牎楠屻€侀敊璇槧灏勶紱鑱婂ぉ/浠ｇ悊/瀹¤濡備綍鎺ョ嚎 |
| 3 | `gateway/service.py` | 100 | 鍞竴鐨勪笟鍔＄紪鎺掞細鎺掗槦 鈫?鍙栧巻鍙?鈫?瑁佸壀 鈫?璋冩ā鍨?鈫?钀藉簱 鈫?鍘婚噸 |
| 4 | `gateway/context.py` | 135 | token 浼扮畻涓庨绠楄鍓紙绾嚱鏁帮紝鏈€瀹规槗鏀癸級 |
| 5 | `gateway/provider.py` | 122 | demo 鍥炴樉涓?SSE 瑙ｆ瀽銆佷笂娓搁敊璇浣曞綊涓€ |
| 6 | `gateway/storage.py` | 506 | 杩佺Щ锛堝璁?璐﹀彿/浼氳瘽锛夈€佽〃缁撴瀯銆佷簨鍔¤竟鐣屻€佹煡璇?|
| 7 | `gateway/tickets.py` | 62 | 涓€娆℃€хエ鎹紙30 绉掋€佸崟娆°€佺粦瀹氳祫婧愶級 |
| 8 | `gateway/access_log.py` | 198 | 璁块棶鏃ュ織涓棿浠躲€佸弬鏁版憳瑕佷笌鑴辨晱 |
| 9 | `gateway/logging_setup.py` | 303 | JSON 鏍煎紡鍖栥€佹寜绫诲埆/鏈嶅姟鍒嗘枃浠躲€侀槦鍒楀啓鐩樸€佷繚鐣欑瓥鐣?|
| 10 | `gateway/audit.py` | 83 | 瀹¤鍙屽啓锛圖B + 鏂囦欢锛変笌鏌ヨ鎶曞奖 |
| 11 | `gateway/auth.py` | 132 | scrypt 瀵嗙爜銆佷細璇?token銆佺櫥褰曢檺閫熴€丳rincipal |
| 12 | `gateway/admin.py` | 84 | 鍛戒护琛屽缓鐢ㄦ埛 / 鍒楃敤鎴?/ 閲嶇疆瀵嗙爜 |
| 13 | `gateway/proxy.py` | 487 | 鍙嶅悜浠ｇ悊锛氳矾鐢辫〃鏍￠獙銆佸姞鏉冭疆璇笌鍐峰嵈銆丠TTP 娴佸紡杞彂銆乄S 妗ユ帴 |
| 14 | `gateway/proxy_health.py` | 178 | 涓诲姩鎺㈡椿锛氬懆鏈熸帰娴嬨€佸仴搴风姸鎬佹満銆佺姸鎬佸揩鐓?|
| 15 | `gateway/config.py` | 133 | 閰嶇疆椤逛笌鍚姩鏍￠獙 |
| 16 | `web/chat.html` / `web/login.html` | 330 / 绾?100 | 鑱婂ぉ椤典笌鐧诲綍椤碉紙鍧囨棤澶栭儴渚濊禆锛?|

鍚堣绾?3,100 琛?Python锛堝惈娉ㄩ噴涓庢枃妗ｅ瓧绗︿覆锛? 绾?430 琛岄〉闈紝涓嶅惈娴嬭瘯銆?琛屾暟浼氭紓绉伙紝鐢ㄥ畠鍒ゆ柇"璇ュ厛璇诲摢涓?锛屼笉瑕佸綋浣滅簿纭寚鏍囥€?
涓嶇啛鎮夌殑鐘舵€佹満銆侀敊璇垎灞傘€佸苟鍙戞ā鍨嬶紝鍏堢湅 [architecture.md](architecture.md)銆?
## 3. 娴嬭瘯

### 3.1 缁撴瀯

`tests/test_gateway.py`锛堢害 1,080 琛岋紝55 涓敤渚嬶級瑕嗙洊涓氬姟閾捐矾锛宍tests/test_logging.py`锛堢害 170 琛岋紝8 涓敤渚嬶級瑕嗙洊鏃ュ織锛?`tests/test_audit.py`锛堢害 100 琛岋紝5 涓敤渚嬶級瑕嗙洊瀹¤锛宍tests/test_auth.py`锛堢害 180 琛岋紝10 涓敤渚嬶級瑕嗙洊鏈湴璁よ瘉銆傚師鍒欐槸**浠庣湡瀹炲叆鍙ｉ獙璇?*锛?
- 鐢?`fastapi.testclient.TestClient` 璧扮湡瀹炵殑 HTTP 涓?WebSocket 璺緞锛屼笉鐩存帴璋冪敤 `ChatService`锛?- 妯″瀷鏈嶅姟鐢ㄦ湰鍦?`ThreadingHTTPServer` 鏇胯韩锛坄ModelStub`锛夛紝鍙嶅悜浠ｇ悊鍒欑敤 `EchoStub`锛圚TTP锛変笌
  `WsEchoUpstream`锛堢嫭绔嬬嚎绋嬮噷鐨?`websockets` 鏈嶅姟锛夛紝鍙湪闇€瑕?涓婃父琛屼负"鏃朵娇鐢紱
- 姣忎釜鐢ㄤ緥涓€涓?`tempfile.TemporaryDirectory` + 鐙珛 SQLite 鏂囦欢锛屼簰涓嶅共鎵帮紱
- 灏戞暟绾嚱鏁帮紙浼扮畻銆佽鍓€佺エ鎹€侀厤缃牎楠岋級鐩存帴鍗曟祴锛屼笉缁?HTTP銆?
### 3.2 妯″瀷鏇胯韩鑳介€犲嚭鐨勫満鏅?
`ModelStub` 渚濇嵁璇锋眰閲屾渶鍚庝竴鏉℃秷鎭殑鍐呭锛坄content`锛夊喅瀹氳涓猴細

| 杈撳叆 | 涓婃父琛屼负 | 鐢ㄦ潵楠岃瘉 |
| --- | --- | --- |
| 鏅€氭枃鏈?| 姝ｅ父 SSE锛氭敞閲婅 + 澶氳 `data` + `finish_reason: stop` + `[DONE]` | 姝ｅ父閾捐矾銆丼SE 杈圭晫瑙ｆ瀽 |
| `[http-error]` | 鐩存帴杩斿洖 HTTP 500 | 涓婃父閿欒鏄犲皠涓?`502 model_http_error` |
| `[truncated]` | 鍙戝畬鍗婂彞灏辨柇 | 缂?`[DONE]` 鈫?`502 model_incomplete` |
| `[slow]` | 鍙戠涓€涓閲忓悗闃诲锛岀洿鍒版祴璇曟斁琛?| 骞跺彂銆佷細璇濇帓闃熴€佹柇杩炰腑鏂?|

"澶氳 `data`"涓?娉ㄩ噴琛?鏄埢鎰忛€犵殑锛岀敤鏉ヨ鐩栫湡瀹?SSE 閲屽鏄撹俯鐨勮В鏋愰敊璇€?
### 3.3 瑕嗙洊鐭╅樀

| 棰嗗煙 | 鐢ㄤ緥 |
| --- | --- |
| 澶氳疆涓婁笅鏂囥€佸幓閲嶃€侀噸鍚鐢?| `test_http_context_idempotency_and_restart` |
| WS 娴佸紡涓?HTTP 閲嶆斁浜掗€?| `test_websocket_stream_and_http_replay` |
| 浼氳瘽鍐呬覆琛?/ 璺ㄤ細璇濆苟琛?| `test_session_serialization_across_transports_and_parallel_sessions` |
| 鏂繛 鈫?`interrupted` | `test_websocket_disconnect_records_interrupted_run` |
| 宕╂簝鎭㈠锛堝惎鍔ㄤ慨澶嶏級 | `test_restart_marks_unfinished_durable_run_interrupted` |
| 澶辫触涓嶆薄鏌撲笂涓嬫枃 | `test_failed_provider_requests_do_not_pollute_next_context` |
| 閴存潈銆侀潪娉曡緭鍏ャ€佹湭鐭ュ瓧娈?| `test_authentication_and_invalid_inputs` |
| 绠＄悊鎺ュ彛锛堝鍒犳敼鏌ャ€佸畧鍗級 | `test_agent_management`銆乣test_session_management_and_listing`銆乣test_management_refuses_while_generating` |
| 娓呯┖鍘嗗彶鍚庡鐢?`request_id` | `test_clear_history_allows_reusing_request_id` |
| 鍒嗛〉 | `test_message_listing_pagination` |
| 绁ㄦ嵁锛堝崟娆°€佺粦瀹氥€佽繃鏈燂級 | `test_ticket_grants_websocket_access_once`銆乣test_ticket_is_bound_to_one_session`銆乣test_ticket_store_expires_and_is_single_use` |
| 椤甸潰涓庡仴搴锋鏌ュ厤閴存潈 | `test_chat_page_and_health_need_no_token` |
| 涓婁笅鏂囪鍓?| `test_estimate_tokens_is_conservative`銆乣test_context_keeps_newest_turn_without_splitting_pairs`銆乣test_long_history_is_trimmed_to_budget`銆乣test_short_history_reports_no_trimming`銆乣test_oversized_input_returns_413_and_can_retry`銆乣test_context_window_must_leave_room` |
| 绯荤粺浠ｇ悊 | `test_model_client_ignores_system_proxy_by_default`銆乣test_system_proxy_can_be_enabled_explicitly`銆乣test_system_proxy_setting_is_validated` |
| 閿欒褰㈢姸缁熶竴 | `test_websocket_error_events_share_one_shape`銆乣test_websocket_gateway_error_has_status_and_ids`銆乣test_http_stream_error_uses_error_envelope` |
| 鏁版嵁搴撹縼绉讳笌鏃у簱鍗囩骇 | `test_schema_version_upgrades_legacy_database` |
| 鍙嶅悜浠ｇ悊锛堣浆鍙戙€佸墺鍓嶇紑銆侀壌鏉冦€?02/504銆佸洖鐜€丅OM銆侀厤缃牎楠岋級 | `test_proxy_forwards_http_and_strips_prefix`銆乣test_proxy_can_keep_prefix`銆乣test_proxy_requires_gateway_token`銆乣test_proxy_stays_disabled_without_routes_file`銆乣test_proxy_websocket_passthrough`銆乣test_proxy_upstream_failures`銆乣test_proxy_timeout_maps_to_504`銆乣test_proxy_loop_guard`銆乣test_proxy_route_file_tolerates_bom`銆乣test_proxy_route_config_is_validated` |
| 浠ｇ悊閫夎矾涓庡け璐ヨ涔?| `test_proxy_round_robin_uses_all_upstreams`銆乣test_proxy_weighted_round_robin`銆乣test_proxy_failover_and_cooldown`銆乣test_proxy_post_is_not_replayed_on_connect_error` |
| 浠ｇ悊瑙勫垯 | `test_proxy_longest_prefix_wins`銆乣test_proxy_methods_can_be_limited`銆乣test_proxy_can_add_path_prefix`銆乣test_proxy_websocket_can_be_disabled` |
| 浠ｇ悊 WS 绁ㄦ嵁锛堟祻瑙堝櫒鍑嗗叆锛?| `test_proxy_ticket_grants_browser_websocket`銆乣test_proxy_ticket_rejects_unknown_path` |
| 浠ｇ悊涓诲姩鎺㈡椿 | `test_health_check_skips_unhealthy_upstream`銆乣test_health_check_treats_4xx_as_alive`銆乣test_health_check_is_off_by_default`銆乣test_proxy_status_requires_auth_and_reports_state`銆乣test_health_config_is_validated` |
| 鏃ュ織涓庤劚鏁?| `test_records_path_status_and_redacts_sensitive_fields`銆乣test_long_content_is_previewed`銆乣test_query_ticket_is_redacted`銆乣test_request_body_still_reaches_endpoint`銆乣test_inbound_request_id_is_preserved`銆乣test_params_can_be_disabled`銆乣test_nested_values_and_lists`銆乣test_size_rotation_and_retention_cleanup` |
| 瀹¤ | `test_agent_mutations_are_audited`銆乣test_session_clear_and_delete_are_audited`銆乣test_startup_event_is_recorded`銆乣test_audit_query_requires_token_and_pages`銆乣test_audit_failure_does_not_break_business` |
| 鏈湴璁よ瘉 | `test_hash_and_verify`銆乣test_login_logout_and_cookie_auth`銆乣test_wrong_password_is_audited_and_rate_limited`銆乣test_audit_failure_records_login`銆乣test_csrf_rejects_cross_site_mutation`銆乣test_revoked_and_expired_sessions_are_rejected`銆乣test_legacy_token_still_works_in_local_mode`銆乣test_websocket_accepts_session_cookie`銆乣test_next_redirect_is_restricted`銆乣test_login_page_is_disabled_in_legacy_mode` |
| demo 妯″紡闆朵緷璧?| `test_default_demo_requires_no_model_service` |

### 3.4 鍐欐柊鐢ㄤ緥鏃剁殑鍥涗釜鍧?
1. **`TestClient` 鐨勯粯璁ゅご浼氬甫杩?WebSocket**銆傜敤 `TestClient(app, headers={"Authorization": "Bearer ..."})`
   鍒涘缓鏃讹紝`websocket_connect` 涔熶細甯︿笂杩欎釜澶达紝浜庢槸"绁ㄦ嵁閴存潈"鏍规湰娌¤璧板埌銆傝妯℃嫙娴忚鍣ㄥ繀椤绘樉寮忚鐩栵細
   `client.websocket_connect(url, headers={"Authorization": ""})`銆?2. **`TestClient` 鑷繁蹇界暐浠ｇ悊鐜鍙橀噺**锛屾墍浠ュ彲浠ュ畨鍏ㄥ湴鍦ㄧ敤渚嬮噷 `mock.patch.dict(os.environ, ...)` 娴嬩唬鐞嗚涓猴紱
   鐪熸浼氳蛋浠ｇ悊鐨勬槸 `ModelProvider` 閲岀殑 httpx銆?3. **涓嶈鐢?`time.sleep` 绛夋ā鍨?*銆俙ModelStub.slow_started` / `release_slow` 灏辨槸涓烘瀛樺湪鐨勶細
   绛変簨浠躲€佹斁琛屻€佸啀鏂█锛涘苟鍦?`finally` 閲屽厹搴?`release_slow.set()`锛屽惁鍒欑敤渚嬩細鎸傛銆?4. **鏃ュ織娴嬭瘯瑕?flush 鍐嶈鏂囦欢**銆俙setup_logging` 鐢ㄩ槦鍒楀紓姝ュ啓鐩橈細鍏?`runtime.stop()` 鍋滅洃鍚嚎绋嬶紝
   璇诲畬鏂囦欢鍐嶆仮澶?`root` 鐨?handler/level锛坄tests/test_logging.py` 鐨?`setUp/tearDown` 鏄ā鏉匡級锛?   鍚﹀垯 Windows 涓婃枃浠跺彞鏌勬病鍏充細瀵艰嚧涓存椂鐩綍鍒犱笉鎺夈€?
## 4. 甯歌鏀瑰姩鎬庝箞鍋?
### 4.1 鏂板涓€涓ā鍨嬮€傞厤鍣?
1. 鍦?`provider.py` 澧炲姞鍒嗘敮锛堟垨鏂板缓 `provider_xxx.py`锛屼繚鎸佸悓鏍风殑 `stream(messages, model) -> AsyncIterator[str]` 濂戠害锛夈€?2. 鍙?`yield` 鏂囨湰澧為噺锛涗笉瑕佸湪杩欓噷鍐欐暟鎹簱鎴栨嫾鍘嗗彶銆?3. 鎵€鏈夊け璐ラ兘鏄犲皠鎴?`GatewayError`锛屽苟閫変竴涓凡鏈夌殑鎴栨柊鐨?`code`锛?   - 涓婃父 4xx/5xx 鈫?`502/503`锛?   - 瓒呮椂 鈫?`504 model_timeout`锛?   - 鍗忚涓嶇 鈫?`502 model_protocol_error`銆?4. 鏂板閰嶇疆椤规寜 [4.4](#44-鏂板涓€涓厤缃」) 璧颁竴閬嶃€?5. 琛ヤ竴涓垨涓や釜鐢ㄤ緥锛氭甯告祦 + 鑷冲皯涓€绉嶅け璐ユ祦銆?
### 4.2 鏂板涓€涓?HTTP 鎺ュ彛

```python
@app.get("/sessions/{session_id}/something", dependencies=auth)
async def something(session_id: str):
    await run_in_threadpool(store.get_session, session_id)   # 鍏堢‘璁ゅ瓨鍦?    return await run_in_threadpool(store.some_query, session_id)
```

- 闇€瑕佹牎楠屽氨鐢?Pydantic 妯″瀷锛堢户鎵?`InputModel`锛宍extra="forbid"` 宸插唴缃級锛?- 涓氬姟澶辫触鎶?`GatewayError(status, code, message)`锛屼笉瑕佸啀鍐?`JSONResponse`锛?- 鍚屾鐨勬暟鎹簱璋冪敤鍔″繀鐢?`run_in_threadpool`锛屽惁鍒欎細闃诲浜嬩欢寰幆锛?- 鏀瑰畬鍚屾 [api.md](api.md) 鐨勭鐐瑰拰閿欒鐮佽〃銆?
### 4.3 鏂板浜嬩欢瀛楁

1. `service.py` 閲屽湪 `yield` 鐨勫瓧鍏镐腑鍔犲瓧娈碉紙`identifiers` / `context_info` 鍙鐢級銆?2. HTTP 鐨?`chat` 浼氳嚜鍔ㄦ妸瀹冩斁杩涘搷搴斾綋锛沇S 鐨?`send_json` 浼氳嚜鍔ㄩ€忎紶銆?3. `web/chat.html` 鑻ラ渶瑕佸睍绀猴紝鍦?`onmessage` 閲岃 `data.<瀛楁>`銆?4. 琛ユ祴璇曟柇瑷€锛屽苟鍦?[api.md 搂3.3](api.md#33-浜嬩欢) 鏇存柊浜嬩欢琛ㄣ€?5. 鏂板瓧娈靛繀椤?*杩藉姞**锛屼笉瑕佹敼鍚嶆垨鍒犻櫎宸叉湁瀛楁锛堝绾︾ǔ瀹氭€ц [api.md 搂6](api.md#6-濂戠害绋冲畾鎬?锛夈€?
### 4.4 鏂板涓€涓厤缃」

浜斿閮借鏀癸紝婕忎竴澶勫氨浼氬嚭鐜?閰嶄簡浣嗕笉鐢熸晥"锛?
| 鏂囦欢 | 鏀逛粈涔?|
| --- | --- |
| `gateway/config.py` | `Settings` 鍔犲瓧娈碉紙甯﹂粯璁ゅ€硷級+ `__post_init__` 鏍￠獙 + `from_env` 瑙ｆ瀽 |
| `.env.example` | 鍔犱竴琛岋紝甯︽敞閲婅鏄庡彇鍊间笌褰卞搷 |
| `README.md` | "閰嶇疆鍙傝€?琛ㄥ姞涓€琛?|
| 浣跨敤澶?| 浠?`settings.xxx` 璇伙紝涓嶈鐩存帴璇?`os.environ` |
| `tests/` | 鑷冲皯涓€涓粯璁ゅ€?闈炴硶鍊兼柇瑷€ |

瑙ｆ瀽瑕佹敞鎰忥細鏁板€煎瀷鐢?`int/float` 骞剁粰涓枃閿欒鎻愮ず锛涘竷灏斿瀷缁熶竴鐢?`read_flag()`锛堟帴鍙?`true/1/yes/on`锛岄潪娉曞€兼姤閿欒€屼笉鏄潤榛?False锛夈€?
### 4.5 鏂板琛ㄦ垨瀛楁

鏁版嵁搴撶粨鏋勭敱 `gateway/storage.py` 鐨?`MIGRATIONS` 绠＄悊锛氭瘡椤规槸 `(鐗堟湰鍙? 璇彞鍏冪粍)`锛?`Store.initialize()` 浼氭寜鐗堟湰鍙蜂粠灏忓埌澶цˉ榻愬苟鍐欏叆 `schema_version` 琛ㄣ€傚姞瀛楁灏辨槸鍦?*鏈熬杩藉姞**涓€涓増鏈細

```python
MIGRATIONS: list[tuple[int, tuple[str, ...]]] = [
    (1, (...)),                                    # 宸插彂甯冿紝姘镐笉淇敼
    (2, (
        "ALTER TABLE runs ADD COLUMN token_usage INTEGER",
    )),
]
```

涓夋潯瑙勫垯锛?
1. **鍙拷鍔狅紝涓嶄慨鏀瑰凡鍙戝竷鐨勬潯鐩?*銆傛棫搴撴鏄潬杩欎簺璇彞浠庣増鏈?0 閫愮骇鍗囧埌鏈€鏂帮紝鏀逛簡灏卞崌涓嶄笂鍘汇€?2. 鏂拌鍙ュ繀椤昏兘瀹夊叏浣滅敤鍦?*宸叉湁鏁版嵁**涓婏細鍔犲垪瑕佷箞鍏佽 `NULL`銆佽涔堝甫 `DEFAULT`锛涘姞琛ㄧ敤 `IF NOT EXISTS`銆?3. 鐗堟湰鍙峰繀椤讳弗鏍奸€掑涓斾笉閲嶅锛坄_migrate` 鎸夐『搴忓垽鏂?`version > current`锛夈€?
楠岃瘉鏂瑰紡锛氱敤鏃у簱鏂囦欢璺戜竴娆?`python run.py`锛屾垨鐓ф妱 `test_schema_version_upgrades_legacy_database` 鐨勫啓娉?锛堥€犱竴涓病鏈?`schema_version` 鐨勫簱 鈫?`initialize` 鈫?鏂█鐗堟湰鍙蜂笌鏃ф暟鎹兘鍦級銆?
娌℃湁鍥炴粴鑴氭湰锛氳縼绉诲彧杩涗笉閫€銆傞渶瑕侀檷绾ф椂鍙兘鏀逛唬鐮佹垨浠庡浠芥仮澶嶏紝鎵€浠ュ姩鐢熶骇搴撳墠鍏堝浠姐€?
### 4.6 淇敼瑁佸壀绛栫暐

`context.py` 鐨勪笁涓笉鍙橀噺涓嶈兘鐮达細

1. 鍙戝嚭鍘荤殑娑堟伅浼扮畻鎬诲拰 鈮?`budget_tokens`锛?2. 鍘嗗彶淇濇寔"鐢ㄦ埛寮€澶淬€佹垚瀵瑰嚭鐜般€佷弗鏍间氦鏇?锛?3. 鏁版嵁搴撻噷鐨勫巻鍙蹭笉琚慨鏀癸紙鍙奖鍝嶆湰娆¤姹傦級銆?
鏀瑰姩鍚庤嚦灏戝悓姝ワ細

- `test_context_keeps_newest_turn_without_splitting_pairs`锛堢畻娉曪級锛?- `test_long_history_is_trimmed_to_budget`锛堢鍒扮涓庝簨浠跺瓧娈碉級锛?- README 鐨勫叕寮忎笌 [api.md 搂5](api.md#5-context-瀵硅薄) 鐨勫瓧娈靛惈涔夈€?
### 4.7 鎶婂彟涓€涓」鐩寕鍒扮綉鍏冲叆鍙ｏ紙涓嶇敤鏀逛唬鐮侊級

1. 纭涓婃父宸茬粡鍦ㄨ窇锛屼緥濡?`http://127.0.0.1:9001`锛?2. 澶嶅埗 `routes.json.example` 涓?`routes.json`锛屽姞涓€鏉?`{"prefix": "/finance/", "target": "http://127.0.0.1:9001"}`锛?3. 閲嶅惎缃戝叧锛岃闂?`http://127.0.0.1:8000/finance/`銆?
闇€瑕佺粰涓婃父甯﹀嚟鎹椂鍔?`"headers": {"X-Api-Key": "..."}`锛涘笇鏈涗繚鐣欏墠缂€灏辨妸 `strip_prefix` 璁句负 `false`锛?涓婃父瑕佹眰 `/api/...` 灏卞姞 `"add_prefix": "/api"`锛涘彧寮€鏀鹃儴鍒嗘柟娉曠敤 `"methods": ["GET"]`銆?澶氫釜瀹炰緥灏卞啓 `"targets": [{"url": "...", "weight": 1}, ...]`锛屾寜鏉冮噸杞骞惰嚜鍔ㄥ喎鍗存晠闅滀笂娓搞€?鍓嶇紑涓嶈兘涓庡唴缃帴鍙ｅ啿绐侊紙`/sessions/` 绛夛級锛岄厤缃啓閿欐椂鍚姩浼氱洿鎺ユ姤閿欍€俙routes.json` 宸茶蹇界暐锛屼笉浼氳繘浠撳簱銆?
娴忚鍣ㄨ杩炶浠ｇ悊鐨?WebSocket 鏃讹紝椤甸潰鍏?`POST /proxy-ticket` 鎹㈢エ锛堢エ鎹粦瀹氶偅鏉＄簿纭矾寰勶級锛?鍐嶄互 `?ticket=` 杩炴帴锛涚エ鎹笉浼氳浆鍙戠粰涓婃父銆?
鎯宠缃戝叧鑷姩鎽樻帀鍧忎笂娓革紝灏卞湪 `routes.json` 椤跺眰鍔?`"health": {"enabled": true}`锛堟垨鍦ㄥ崟鏉¤矾鐢遍噷瑕嗙洊锛夛紝
鐒跺悗鐢?`GET /proxy-status` 鐪嬫瘡涓笂娓哥殑 `healthy` / `last_error`銆傛帰娲诲弬鏁颁笌鍒ゅ畾瑙勫垯瑙?[proposals/health-checks.md](proposals/health-checks.md)銆?
鑻ユ兂鏀逛唬鐞嗙殑閫夎矾鎴栧け璐ヨ涔夛紝鏀?`proxy.py` 鐨?`_attempts` / `_cool`锛屽苟鍚屾 `test_proxy_*` 涓?[api.md 搂2.8](api.md#28-鍙嶅悜浠ｇ悊鍓嶇紑鎸?routesjson-鍔ㄦ€佹敞鍐?銆?
## 5. 璋冭瘯鎵嬫硶

| 鎯崇‘璁?| 鍋氭硶 |
| --- | --- |
| 鎺ュ彛闀夸粈涔堟牱 | 鎵撳紑 `http://127.0.0.1:8000/docs` |
| 鏈嶅姟鏄惁娲荤潃 | `curl http://127.0.0.1:8000/health`锛堟垨鐢?Invoke-RestMethod锛?|
| 涓婁竴杞埌搴曞彂鐢熶簡浠€涔?| `GET /sessions/{id}/requests/{request_id}`锛岀湅 `status` / `error_code` |
| 鍘嗗彶閲屾湁浠€涔?| `GET /sessions/{id}/messages`锛屾敞鎰?`run_status` |
| 鏁版嵁搴撳師濮嬪唴瀹?| 瑙佷笅闈㈢殑涓€琛屽紡鏌ヨ |
| 妯″瀷鍒板簳鏀跺埌浜嗕粈涔?| 鍦?`provider.py` 鐨?`payload` 涓婁复鏃舵柇鐐?鎵撳嵃锛?*鍒彁浜?*锛夛紝鎴栫敤娴嬭瘯鏇胯韩鏂█ |

```powershell
# 涓嶈 sqlite3 鍛戒护琛屼篃鑳芥煡锛氱洿鎺ョ敤 Python
& "...\python.exe" -c "import sqlite3;c=sqlite3.connect(r'data/gateway.sqlite3');c.row_factory=sqlite3.Row;[print(dict(r)) for r in c.execute('SELECT id,status,error_code,request_id FROM runs ORDER BY created_at DESC LIMIT 5')]"
```

鎺掓煡椤哄簭寤鸿锛氬厛鐪嬭繍琛岃褰曠殑 `status` 涓?`error_code`锛堜笟鍔℃槸鍚︽垚鍔燂級锛屽啀鐪嬫湇鍔℃棩蹇楋紙寮傚父绫诲瀷锛夛紝
鏈€鍚庢墠鎬€鐤戞ā鍨嬫湇鍔℃湰韬紙鐢?`curl` 鐩存帴鎵?`MODEL_BASE_URL/chat/completions` 楠岃瘉锛夈€?
## 6. 鎻愪氦鍓嶆鏌ユ竻鍗?
- [ ] `python -m unittest discover -s tests` 鍏ㄧ豢锛堝綋鍓?78 涓紱闃舵 4/5 瀹屾垚鍚庡啀鏇存柊锛?- [ ] `python -m compileall -q gateway tests examples run.py` 閫氳繃
- [ ] 鏀逛簡鎺ュ彛/浜嬩欢/閿欒鐮?鈫?鍚屾 `docs/api.md`
- [ ] 鏀逛簡琛屼负鎴栭厤缃?鈫?鍚屾 `README.md` 涓?`.env.example`
- [ ] 鏀逛簡鏋舵瀯鎬у彇鑸?鈫?鍦?`docs/design-decisions.md` 琛ヤ竴鏉?ADR
- [ ] 娌℃湁鎶?`.env`銆乣data/`銆乣routes.json`銆乣__pycache__/` 甯﹁繘鎻愪氦锛坄.gitignore` 宸茶鐩栵級
- [ ] 鏃ュ織閲屾病鏈夌敤鎴峰唴瀹广€佷笂娓稿搷搴旀垨瀵嗛挜
- [ ] 鏂板鐨勯厤缃」鍦?README 閰嶇疆琛ㄩ噷鏈夎鏄?
