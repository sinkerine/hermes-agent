"""Small model schema and session-scoped entry point for Matrix interactive cards."""

from tools.registry import tool_error, tool_result

from . import TOOLSET

PRESENT_UI_SCHEMA = {
    "name": "present_ui",
    "description": (
        "Present one interactive card in this Matrix conversation. Use when structure or interaction helps. "
        "Use meaningful labels; never include secrets, credentials or generated executable code. "
        "The card contains the answer: follow with at most one sentence. A backend action arrives as a new user turn. "
        "After two errors answer in plain text. Hermes owns version/ui_id/revision/status; fields inside ui are ignored. "
        "To revise a card use its returned replace_ui_id; status active/expired/superseded is only for revisions. "
        "UI: root references a node id; nodes is a flat array, all nodes reachable, ids unique. state maps binding keys "
        "to JSON values. Nodes: stack(children,direction vertical|horizontal,gap); heading(text,level 1-3); text(text); "
        "rich_text(text,html optional sanitized); divider; badge(text,tone); key_value(items[{label,value}]); card(children,title?); "
        "list(items[{text,secondary?}],ordered); image(alt,mimetype,url mxc:// or file encrypted attachment, or upload absolute local "
        "PNG/JPEG/GIF/WebP path; no remote image fetch); link(label,url HTTPS); button(label,action); "
        "table(columns[{key,label,kind? text|number}],rows objects); chart(kind bar|line|pie|scatter, "
        "series[{name,values? or points:[{x,y,label?}]}],categories?); graph(layers[{label}],nodes[{id,label,layer,detail?}],edges[{from,to,label?}], "
        "bind_selected?); carousel(label,items[{id,image:{alt,mimetype,url|file|upload},title,subtitle?,meta?,action?}]); "
        "select(label,bind,options[{value,label}]); choice(label,bind,options,multiple?); toggle(label,bind); "
        "text_input(label,bind,max_length,multiline?,placeholder?,required?); "
        "number_input(label,bind,min?,max?,step?,display? field|slider); form(children,action_id,submit_label,single_use? default true); "
        "tabs(tabs[{value,label,child}],bind); collapsible(label,children,bind?). "
        "Actions: {type:'submit',action_id,include:[state keys],single_use?} or {type:'open_url',url HTTPS} "
        "or {type:'set_state',key,value} or {type:'toggle_state',key}. Nodes may have visible_when:{state:key,equals:value}. "
        "Limits: 40000 UTF-8 payload bytes, 44000 complete content bytes, 45000 edit bytes; no truncation. "
        "Errors identify the exact field path and constraint to correct."
    ),
    "parameters": {
        "type": "object", "required": ["ui"],
        "properties": {
            "ui": {
                "type": "object", "required": ["state", "root", "nodes"],
                "properties": {
                    "title": {"type": "string"}, "status_text": {"type": "string"},
                    "state": {"type": "object", "additionalProperties": True}, "root": {"type": "string"},
                    "nodes": {"type": "array", "items": {
                        "type": "object", "required": ["id", "type"],
                        "properties": {"id": {"type": "string"}, "type": {"type": "string", "enum": [
                            "stack", "heading", "text", "rich_text", "divider", "badge", "key_value", "list",
                            "image", "link", "button", "table", "chart", "graph", "carousel", "select", "choice",
                            "toggle", "text_input", "number_input", "form", "tabs", "collapsible", "card"]}},
                        "additionalProperties": True}},
                }, "additionalProperties": True,
            },
            "summary": {"type": "string", "description": "Short revision summary"},
            "replace_ui_id": {"type": "string", "description": "ui_id returned for this conversation"},
            "status": {"type": "string", "enum": ["active", "expired", "superseded"]},
        }, "additionalProperties": False,
    },
}


def register_present_ui(ctx) -> None:
    from ..adapter import MatrixAdapter, matrix_deps_present
    from agent.delegation_context import is_delegated_child_process_context

    def handle(args):
        if is_delegated_child_process_context():
            return tool_error("delegated_child: Matrix conversations only")

        async def present(target):
            adapter = target.adapter
            controller = getattr(adapter, "_interactive_ui_controller", None)
            if target.platform != "matrix" or not isinstance(adapter, MatrixAdapter) or controller is None:
                return {"ok": False, "error": "not_matrix_conversation: Matrix conversations only"}
            return await controller.present(chat_id=target.chat_id, thread_metadata=target.metadata,
                                            session_key=target.session_key, ui=args.get("ui"),
                                            summary=args.get("summary"), replace_ui_id=args.get("replace_ui_id"),
                                            status=args.get("status"))

        try:
            result = ctx.deliver_to_conversation(present)
        except Exception:  # health: allow BLE001 -- registry logs uncaught exceptions with private payload details
            return tool_error("interactive_ui: : bad_value: Unable to process card")
        if result.get("ok"):
            return tool_result(result)
        error = result.get("error", "delivery_failed")
        if error in ("no_conversation", "unsupported_surface", "gateway_unavailable"):
            error = "not_matrix_conversation: Matrix conversations only"
        return tool_error(error)

    ctx.register_tool(name="present_ui", toolset=TOOLSET, schema=PRESENT_UI_SCHEMA, handler=handle,
                      check_fn=matrix_deps_present, description="Matrix interactive cards", emoji="🃏")
