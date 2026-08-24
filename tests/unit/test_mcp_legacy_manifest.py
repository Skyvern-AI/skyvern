from __future__ import annotations

import hashlib
import json
from typing import Any

import pytest

from skyvern.cli.mcp_tools import mcp
from skyvern.cli.mcp_tools.instructions import DEFAULT_INSTRUCTIONS
from skyvern.cli.mcp_tools.scopes import MCPScope, apply_scope

DEFAULT_INSTRUCTIONS_SHA256 = "2c3698446bf4df160c10e033683b1fd42125a33859373e40f8fe1f222e9f9bcc"

# Digests snapshot each legacy tool's wire manifest: name, schema, description, annotations, _meta.
# New tool names are ignored; any change to a legacy tool fails.
# Regenerate after an INTENTIONAL change: rebuild `actual` exactly as test_legacy_tool_manifests_are_frozen
# does (same _digest over name/inputSchema/description/annotations/_meta) and paste the dict; then justify
# the drift per tool in the PR, since every entry here is a wire-visible tools/list change.
LEGACY_TOOL_MANIFEST_DIGESTS = {
    "skyvern_act": "407b30ac46dc9cbf83e1eca17c7eef3c19bf850ef8a87b76a3f089dce114d669",
    "skyvern_bitwarden_config_clear": "107c38d2c0da3a2f2fcc0b75935dcfe2b9d24ef1f7e369e22834ef23d6cd4ea3",
    "skyvern_bitwarden_config_get": "da79546595e7667c65a5e45b8625c8712026adb71b4d2dfc627621af1a2577c4",
    "skyvern_bitwarden_config_set": "b454d21e59f7421d14983aa02b64e0fa897ee03af3888114df19ddd6a91bf2cc",
    "skyvern_bitwarden_items": "abf727ff755578e4dc4a475643ca1477a7c923c4009edca27b782afbfb63cd30",
    "skyvern_block_schema": "e7527dcb32d072814b3660ad5b1b837e957bd1605c872f8340cbc670fe59d2f5",
    "skyvern_block_validate": "f93cbef9b0098f3469af49b6762fef337576895d48e442b7933f4b7d378c1f4d",
    "skyvern_browser_profile_create": "8d118cf839a408e2d135eb3a48cc09baa253a7eb42342a86f203a086829445c2",
    "skyvern_browser_profile_delete": "ade127a22df9c841510a4435589b4cdbbab8527402774357d7c0d688168bdeb6",
    "skyvern_browser_profile_get": "4e78265e134bb6d587c7db30b873a56173cad9d7409c8c724aeae9e55d7c58b7",
    "skyvern_browser_profile_list": "8ee920abdeec36c9712aec3d7c6a883550242164c77eb0211d2550716f477d44",
    "skyvern_browser_profile_update": "e83c3227ef13e827bd0c7d6e23bb0e03fefe2fe83e73bfa4b3ca696d151b2b61",
    "skyvern_browser_session_close": "d0923948805b1bbccd7803df1a3748426263a8bf630cf9fcbee5a21f2f463825",
    "skyvern_browser_session_connect": "7c21d99fbb35dc0199e59367373387dca0e8f8d9dd4615303a3f09f579db2d06",
    # Re-frozen for SKY-13272: `timeout` is now rejected above MAX_TIMEOUT instead of clamped, so the param
    # description says "max 240" rather than "capped at 240". Description text only; the schema is unchanged.
    "skyvern_browser_session_create": "6244d9614c04f403ee9db0c6b0cdee10f5be121cf85979b069a106044b08b161",
    "skyvern_browser_session_get": "fdf579ac23ce28eea5e243a407a8f810fdcca6ed491c2da7fbe56836b1474109",
    "skyvern_browser_session_list": "dac429184440a1f76d2a787fb81793e37220408d749f0d3cacfbd29222e55f19",
    "skyvern_clear_local_storage": "8a6b80bfdbe3b1e2e36f7f485cb5bb7ff527594e37536816c2f6b614852078f5",
    "skyvern_clear_session_storage": "dc65cded7c6c8d4252f546c53248f94246574313a56c7a5bbe2df0de69bb6059",
    "skyvern_click": "efba4394a9c0b6bf39f480e0ab97cded9bcc40de92f5ceb53a3ab26bc1fa40d1",
    "skyvern_clipboard_read": "fca638745f40e3d65cd61603037c7c94803373d86f68a3879a3084ed467331a2",
    "skyvern_clipboard_write": "e766520b40a9edffa84ecaff725856e89a8bb963b49f512447a25b962ca54c90",
    "skyvern_code_block_lint": "bfe1a5c65f9f7769b53029064aa127c54abc501d991a1c7d5fe8879e38b4f152",
    # Re-frozen against main: #14971 (SKY-13929) changed a param description upstream. Not a lean-scope change.
    "skyvern_code_block_synthesize": "c707c194228d1c60625d662c975b478f222ef5e70f108ae9dc6a9fae9e41ab59",
    "skyvern_console_messages": "9ae534a61acf10fec6fae84dfdcda100e0f97cc8febbdfe7a73f82fcf37d07fa",
    "skyvern_credential_delete": "e3c642d2bba8025f90eb0641655b22c0b709effb601605802ecea536431bc505",
    "skyvern_credential_get": "b91bc6df14644a3a9b47e61ba05623652d208dbee58d75b595863112261c26b6",
    "skyvern_credential_list": "25377204b867b5ad92e7a18b49a081dc84a109002841263c5c2132857b6e5dac",
    "skyvern_drag": "58b53906edc106537f2c799da2bf5e1b8991ec9ce6a3ac5df01827aaf91b3564",
    "skyvern_evaluate": "d25b3741856ed39bad3bb0e1cf76abad3f8a6e28e263b55befb1798cc7e93076",
    "skyvern_evaluate_and_screenshot": "2e886e466be8ed71e003f3f0c06d4ac7cb627a7eb4b2d8698a4cc0694d258de6",
    "skyvern_execute": "2cb175e508b11a4e40564d2057a0725566fc1ba72080ac789512e350b8b0d532",
    "skyvern_extract": "1a6323ec190bb05a00ff1739fc8e379fc6325e4ed16b4058d532743976c3f5dc",
    "skyvern_extract_and_screenshot": "005995f97c6a8078bd1f589d78415c51e7a1189752da35ea2f56c7da902bee01",
    "skyvern_file_upload": "af20707d0c1624561647f46557eae001090104737fe97f980dff8bd54e404cdd",
    "skyvern_find": "19e8bc37c0e1f2d5efd6942a4e831ccdd17e0375200fb933f1506b0821d1092d",
    "skyvern_folder_create": "07e089386143f833dea9ce85112ab4229be3f9e4f2800b1e5954e2cef14aa6b5",
    "skyvern_folder_delete": "9f244733dba37c048acfa3237a8d7e5c9f7bbd44261e303886d980b85098ef7c",
    "skyvern_folder_get": "971ff65b6221f55e31c465bde5bc32129ff10abec29281280b0699037bbaded2",
    "skyvern_folder_list": "799a5bfbde04f3535b06a189563a9722086a969ff0005c22ed3c78ed9fbbded1",
    "skyvern_folder_update": "5ae336fddff41d2dcd80ee0cf14d9f1d43fea9b4ed4825746c863412ea4eb5d9",
    "skyvern_frame_list": "0ae7aba272ccfebe27eba03f4f4463d355fe17e27162e9ed99b9cba7379750b2",
    "skyvern_frame_main": "f934343c7aa4c4fbaf6b172cf626620cd41ac3c102fe903f6df27f69062d1ac1",
    "skyvern_frame_switch": "104275655a254f3c76b7f7c4ba5c72adcdc58912eb080ac1be154863dd96086c",
    "skyvern_get_errors": "c2842b978360a17d9ff019225913adeb113850a0d5de9f7dd9817e568e1f07a6",
    "skyvern_get_html": "e0d81df5626c8a78570409bc7add4e8fce3309f2ad4b774ad700295655a46664",
    "skyvern_get_session_storage": "0f95c9be50dcc2da6fcf71ccef5b8e84ad8f5fc4b0ed8fd87d308145880e6062",
    "skyvern_get_styles": "37a8cf4eb753ebff12d9fe28b6f58e22c030a5fa33b6bb140767ce7fc00de50d",
    "skyvern_get_value": "70f46f2c5cede165997631fb2dc82fc8a572c78f5ba6c00a7b7298cc523d397d",
    "skyvern_handle_dialog": "85953ddcdbdf9f526767d3ec66a821473c2d3170f4d1aaadea263bc0db72c0c6",
    "skyvern_har_start": "b2f70d8ead9b17caf850651f6846195b790308f25b979807c07c51c6ce182f4e",
    "skyvern_har_stop": "8055b6b0e6d802436b8e92a3838eb0f000d00d2d6eb53b4a9316e7a1a103b5f6",
    "skyvern_hover": "f70ab88acb034caf0c20edd08b9f601a00ad31710ed6ff60935c5163e8c152ab",
    "skyvern_login": "16d39b5015531fe80cbfd5c40cd3b699c5c325a48c2bd5416a0a4b0b151a9074",
    "skyvern_navigate": "c59ad7604d90a5c62caf89e779cd4cd7002a17067dfed1622a03c9987c62945b",
    "skyvern_navigate_and_screenshot": "c07c89e60429c0f2e10e53e6ea493de8438357b09437dafe652b5af83432f391",
    "skyvern_navigate_extract_and_screenshot": "1b700d5a6d082a91664376adb95284f2f8c7f30a74e7f73e39ef287006fd73a1",
    "skyvern_network_request_detail": "d74674521e56b4c1028c684d8e2d90bd9c3f65f2a233e28717721d0fd11bf477",
    "skyvern_network_requests": "0ea881741bebf2bec8468b3225ebdde4432ddb12e444e2c43a41d44aff89eddc",
    "skyvern_network_route": "89d7310782a90b19aab0600ab842e5ceaa114d75a0efbc79d42e23d28f17bf80",
    "skyvern_network_unroute": "a5c480635e3cb9fc91ed14935a4c654ef375366c0a99882543a8ea54280b5c89",
    "skyvern_observe": "136835d77007121a9c8a493814ab8ece29a8cc73139dfb652157e501b192c558",
    "skyvern_onepassword_config_clear": "3be70bda330d83d0dd67eb1e41d6ba4586a0868f9620a40ead1c3bdf369c6fab",
    "skyvern_onepassword_config_get": "55ec9485f837fbd6c4ac9fc8602ea7c92234ea383ed1aee7b9810b17bba5c62b",
    "skyvern_onepassword_config_set": "78493cf6da057ba5534d0a30102687d75d7517419bd8e08223dadeeb0a91154e",
    "skyvern_onepassword_items": "4b08cbaf5853ff9b2547e058efe664cfdfefb4025d9a8c88f6b6ac8af261283d",
    "skyvern_open_tabs": "9f85d7bff702f3fa5c7558ff3f89bbad8823badba70124ad30c73dbc4d4f8abd",
    "skyvern_org_get": "af99aada703881af2423d36eba5af5a0832eaf51797428f07f2e3dcee66f52a7",
    "skyvern_org_update": "bbb6283207460a33b0d2759d563ecf4a8431fdc8c5cf7dbb15d10d27f36e0951",
    "skyvern_press_key": "ad74ceb98f5b21483940f027747817d21f751eebb781c11f97529c5be968a9c4",
    "skyvern_run_task": "e48efaa780ab1f49c8d5a7c489edf293880448d07e7e0f3d88b65807d559cb23",
    "skyvern_schedule_create": "c96af8fafabe9cd3164f91c1489f1069a5259e9f3c5405f109ad3e16e0c1db71",
    "skyvern_schedule_delete": "ad6fa7d028b0c168e0b12a475c0e3c954d6402843bc1a984ad605d3b3766adae",
    "skyvern_schedule_disable": "c9e5e01c54c1934db991ab4a81a4fa369340cfabc580ac73990b1b71589101ef",
    "skyvern_schedule_enable": "6df661f097e8cfd30c52fe66d66cc4e1a4b3ff555da2978bcc0acc4bf011fe9d",
    "skyvern_schedule_get": "2eb8f151ace8e2c4116399437672bd41836536db9ac52545207d5905535c94c1",
    "skyvern_schedule_list": "4aa92418fe65c6f07ab89f86ef26828e711cb37e178c2b6d90810ddf296187db",
    "skyvern_schedule_list_for_workflow": "a9949b4ddce25849378936b675f59621165b9587e0ee6b516cb8b1e31b64709e",
    "skyvern_schedule_update": "c8736498aef521f0a021ffecae4aeb2972d21e9a6f06e17e474450526d7db1af",
    "skyvern_screenshot": "96f506db0c7780f79d45261d8a432114893130b04160f977214d08865771a2b8",
    "skyvern_script_deploy": "c14adba89381f2e3b6010d44f5a2c66b840b57fc835cfe08ac864aaacba63b8e",
    "skyvern_script_fallback_episodes": "c6f23e8270426a40ff2692ef33dc738b8083e0a38eaf9d0e2e08f93768d645e6",
    "skyvern_script_get_code": "affbb1fea941088e5b9cb764850e95436259c8fd9ed0b6e26542be1bef03f936",
    "skyvern_script_list_for_workflow": "e86f7ce59edb77cb2d821ad2c77383e6e444c42e8257f301761ec4566fd868ef",
    "skyvern_script_versions": "503d354df26d266aaa1df53741e0648698b45e5063f78c2f5a034f8473b3ba3f",
    "skyvern_scroll": "a0869aaec726312b99cd0dc720ea6e3948ce70e0ff8a82082ef6a4c02625963c",
    "skyvern_select_option": "40fe9f92c652fb90252f8d48928e259fe10a813226433e12ebb31224cfa20b41",
    "skyvern_set_session_storage": "1cdfee7cdab1cae10099634f8888b834d73e2d37e251eb78079337c3d91d092d",
    "skyvern_state_load": "0a0e90bab93ce9c33d61cd1e5f9965bf34a516a03936f6ff3e99b8d28fe15dbe",
    "skyvern_state_save": "59ec8d8dddef8e351e3cd786ae4fb5d205dee4c45187ec7fe8d48d3a60429a2f",
    "skyvern_tab_close": "273b18bbace4c75ea1c23ba7056ba0770224a27f6e81aa72c09a925b6a8a0873",
    "skyvern_tab_list": "871df6226eb641a88e8d2cc91a36c357d0472ea2e7d3ebea1660d83517b65425",
    "skyvern_tab_new": "677fc2136550cc9ee2a4328cee890a3ef0439c41c7febc275af195bfd21c0a33",
    "skyvern_tab_switch": "4bad7852b8db4c9e1b7e421e12ad36a59321bdbf98c1203295259c6b6d3cdb96",
    "skyvern_tab_wait_for_new": "2b0131b628f71645137af7ca78af6d997126b0e1e302d8816cbeeba3ab41e0e2",
    "skyvern_trajectory_get": "6c78c6de100a821fc7ec65d860ef42eaef181c573b185345d38090a25f8960a1",
    "skyvern_type": "325c3b15693fa41962347f4267b8b21d36473f456a7fd01b3ccf2bb28ee86877",
    "skyvern_validate": "782d01e1b6b71b98a04fb6b12371578efe241f01c2875ec89dcf33fb2351d6db",
    "skyvern_wait": "7f9b8d8d39762eaa2c2962ab3b1e4b997454d9a1039ee2fee1b5aaa07019786a",
    "skyvern_wait_for_either_state": "b4b127498f91a7d9e1536d11a8993cb9d1cbf7b1557f9bec832afaa127ea1613",
    "skyvern_workflow_cancel": "0ee1a599421c3a116bf09906613574807b7f0a4e7f72b6a79adf6cfb84edb3d1",
    "skyvern_workflow_create": "42dd751b5ba7607a6be052093eea1d986b03e14c02525b9741618865db666189",
    "skyvern_workflow_delete": "4b8b6574f78122c5d6bf8ee12f5b3cedeb83832d08462445889259cfd0c8b861",
    "skyvern_workflow_get": "b9340c5d1cfa0db49431d4e914c0879f15e458d4c6d4879de0d0de653de842da",
    "skyvern_workflow_list": "3b651992b2f20e020305fe308e0b1cde38d330e4dc3ee243887efaa212a2516a",
    "skyvern_workflow_retry": "da8782d457ba3bcc8b87fffb90ce9dbc287943f28260816d699233a7427b6978",
    "skyvern_workflow_run": "9a149c1cc66deb0afe879b00308aa6c750cfbf4a3fe4ed0af0d7eb307d8229cc",
    # Re-frozen for SKY-14441: adds the optional `include_child_runs` param (default false, preserving the
    # existing result set) and says in the description that child runs are excluded by default.
    "skyvern_workflow_run_list": "5ed785968941b6b3ae3421d6d67ef15af365a2b59a9a1bd21c7bc54dff67e205",
    "skyvern_workflow_status": "730fd46aae7cb9e974b631abdca8fdbc5c5c8a77917c22c3bd76735388103487",
    "skyvern_workflow_update": "3931c7a6b3faee202ebb57291fc2a437b838411ff0082be93d3763dbf727d060",
    "skyvern_workflow_update_folder": "52efdfa02cf84cb9995bfbeb6d00562fdd1364e832e297e853d233ff6f68392d",
    "skyvern_write_grid": "84fb23a7e4a2436c9956d3dd756041b2e786368f4b680877fea49a4fc6058a06",
}

# Resolved memberships for the three pre-existing scopes must remain byte-for-byte stable.
LEGACY_SCOPE_MEMBERSHIP_DIGESTS: dict[MCPScope, tuple[int, str]] = {
    "browser": (54, "8c05058f36d5d4472e11f384ed68cabbae0478b3e16d2f7da6c2d07dda21703a"),
    "build": (61, "3d5e09451edd441de5c79f489539a8b0f97679e0e342972c7541757eb4de14a4"),
    "operate": (29, "d5181b03b06212403ce381a6f95a916d12e303987949acd46b8f813f38e413f0"),
}


def _digest(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode()).hexdigest()


@pytest.mark.asyncio
async def test_legacy_tool_manifests_are_frozen() -> None:
    tools = {tool.name: tool for tool in await mcp.list_tools(run_middleware=False)}

    assert LEGACY_TOOL_MANIFEST_DIGESTS.keys() <= tools.keys()
    actual = {}
    for name in LEGACY_TOOL_MANIFEST_DIGESTS:
        tool = tools[name]
        annotations = tool.annotations.model_dump(by_alias=True, exclude_none=False) if tool.annotations else None
        actual[name] = _digest(
            {
                "name": tool.name,
                "inputSchema": tool.parameters,
                "description": tool.description,
                "annotations": annotations,
                # fastmcp publishes tags here, so a tag edit changes tools/list for every client.
                "_meta": tool.to_mcp_tool().meta,
            }
        )

    assert actual == LEGACY_TOOL_MANIFEST_DIGESTS


@pytest.mark.asyncio
async def test_legacy_scope_memberships_are_frozen() -> None:
    original_transforms = list(mcp._transforms)
    try:
        actual: dict[MCPScope, tuple[int, str]] = {}
        for scope in LEGACY_SCOPE_MEMBERSHIP_DIGESTS:
            mcp._transforms.clear()
            apply_scope(mcp, scope)
            names = sorted(tool.name for tool in await mcp.list_tools(run_middleware=False))
            actual[scope] = (len(names), _digest(names))
    finally:
        mcp._transforms[:] = original_transforms

    assert actual == LEGACY_SCOPE_MEMBERSHIP_DIGESTS


def test_default_instructions_are_frozen() -> None:
    assert mcp.instructions == DEFAULT_INSTRUCTIONS
    assert hashlib.sha256(DEFAULT_INSTRUCTIONS.encode()).hexdigest() == DEFAULT_INSTRUCTIONS_SHA256
