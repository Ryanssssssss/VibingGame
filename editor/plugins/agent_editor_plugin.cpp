/**************************************************************************/
/*  agent_editor_plugin.cpp                                               */
/**************************************************************************/

#include "agent_editor_plugin.h"

#include "core/config/project_settings.h"
#include "core/io/dir_access.h"
#include "core/io/file_access.h"
#include "core/io/image.h"
#include "core/io/json.h"
#include "core/os/os.h"
#include "editor/editor_data.h"
#include "editor/editor_interface.h"
#include "editor/editor_main_screen.h"
#include "editor/editor_node.h"
#include "editor/file_system/editor_file_system.h"
#include "editor/gui/editor_file_dialog.h"
#include "editor/script/script_editor_plugin.h"
#include "editor/settings/editor_settings.h"
#include "editor/themes/editor_scale.h"
#include "scene/gui/button.h"
#include "scene/gui/flow_container.h"
#include "scene/gui/grid_container.h"
#include "scene/gui/label.h"
#include "scene/gui/line_edit.h"
#include "scene/gui/option_button.h"
#include "scene/gui/dialogs.h"
#include "scene/gui/rich_text_label.h"
#include "scene/gui/scroll_container.h"
#include "scene/gui/separator.h"
#include "scene/gui/tab_container.h"
#include "scene/gui/text_edit.h"
#include "scene/gui/tree.h"
#include "scene/gui/texture_rect.h"
#include "scene/resources/image_texture.h"
#include "scene/main/http_request.h"
#include "servers/display/display_server.h"

String AgentWorkspace::_u8(const char *p_text) {
	return String::utf8(p_text);
}

void AgentWorkspace::_set_status(const String &p_text, bool p_error) {
	status->set_text(p_text);
	status->add_theme_color_override(SNAME("font_color"), p_error ? Color(1.0, 0.35, 0.35) : Color(0.55, 0.75, 0.95));
}

// Text is always inserted through add_text: model output cannot inject BBCode.
void AgentWorkspace::_append_inline_markdown(const String &p_text, int p_depth) {
	if (p_depth > 8) {
		transcript->add_text(p_text);
		return;
	}
	String plain;
	for (int i = 0; i < p_text.length();) {
		if (p_text[i] == 92 && i + 1 < p_text.length() && String("\\`*_{}[]<>()#+-.!|~").contains(p_text.substr(i + 1, 1))) {
			plain += p_text.substr(i + 1, 1);
			i += 2;
			continue;
		}
		if (p_text[i] == '[') {
			int label_end = p_text.find("](", i + 1);
			int url_end = label_end < 0 ? -1 : p_text.find(")", label_end + 2);
			if (url_end > label_end && label_end > i) {
				String url = p_text.substr(label_end + 2, url_end - label_end - 2);
				if (url.begins_with("https://") || url.begins_with("http://")) {
					transcript->add_text(plain);
					plain = String();
					transcript->push_meta(url);
					_append_inline_markdown(p_text.substr(i + 1, label_end - i - 1), p_depth + 1);
					transcript->pop();
					i = url_end + 1;
					continue;
				}
			}
		}
		String marker;
		if (p_text.substr(i, 2) == "**" || p_text.substr(i, 2) == "__" || p_text.substr(i, 2) == "~~") {
			marker = p_text.substr(i, 2);
		} else if (p_text[i] == '`' || p_text[i] == '*') {
			marker = p_text.substr(i, 1);
		}
		int end = marker.is_empty() ? -1 : p_text.find(marker, i + marker.length());
		if (end > i + marker.length()) {
			transcript->add_text(plain);
			plain = String();
			if (marker == "`") {
				transcript->push_mono();
			} else if (marker == "~~") {
				transcript->push_strikethrough();
			} else if (marker.length() == 2) {
				transcript->push_bold();
			} else {
				transcript->push_italics();
			}
			String content = p_text.substr(i + marker.length(), end - i - marker.length());
			if (marker == "`") {
				transcript->add_text(content);
			} else {
				_append_inline_markdown(content, p_depth + 1);
			}
			transcript->pop();
			i = end + marker.length();
		} else {
			plain += p_text.substr(i++, 1);
		}
	}
	transcript->add_text(plain);
}

void AgentWorkspace::_open_markdown_link(const Variant &p_meta) {
	if (p_meta.get_type() == Variant::STRING) {
		String url = p_meta;
		if (url.begins_with("https://") || url.begins_with("http://")) {
			OS::get_singleton()->shell_open(url);
		}
	}
}

static PackedStringArray _agent_markdown_cells(const String &p_line) {
	PackedStringArray cells;
	String line = p_line.strip_edges().trim_prefix("|").trim_suffix("|");
	String cell;
	bool code = false;
	for (int i = 0; i < line.length(); i++) {
		if (line[i] == 92 && i + 1 < line.length()) {
			cell += line.substr(i, 2);
			i++;
		} else if (line[i] == '`') {
			code = !code;
			cell += "`";
		} else if (line[i] == '|' && !code) {
			cells.push_back(cell.strip_edges());
			cell = String();
		} else {
			cell += line.substr(i, 1);
		}
	}
	cells.push_back(cell.strip_edges());
	return cells;
}

static bool _agent_markdown_separator(const String &p_line, int p_columns) {
	PackedStringArray cells = _agent_markdown_cells(p_line);
	if (cells.size() != p_columns || p_columns < 2 || p_columns > 32) {
		return false;
	}
	for (const String &cell : cells) {
		String rule = cell.trim_prefix(":").trim_suffix(":");
		if (rule.length() < 3 || !rule.replace("-", "").is_empty()) {
			return false;
		}
	}
	return true;
}

void AgentWorkspace::_append_markdown(const String &p_text) {
	bool in_code = false;
	PackedStringArray lines = p_text.replace("\r\n", "\n").split("\n");
	for (int i = 0; i < lines.size(); i++) {
		String line = lines[i];
		if (line.strip_edges().begins_with("```")) {
			in_code = !in_code;
			continue;
		}
		if (in_code) {
			transcript->push_mono();
			transcript->add_text(line + "\n");
			transcript->pop();
			continue;
		}
		PackedStringArray header = _agent_markdown_cells(line);
		if (i + 1 < lines.size() && line.contains("|") && _agent_markdown_separator(lines[i + 1], header.size())) {
			transcript->push_table(header.size());
			int row = 0;
			while (true) {
				PackedStringArray cells = _agent_markdown_cells(lines[i]);
				for (const String &cell : cells) {
					transcript->push_cell();
					transcript->set_cell_border_color(Color(0.5, 0.5, 0.5, 0.5));
					transcript->set_cell_padding(Rect2(6, 4, 6, 4));
					if (row == 0) { transcript->push_bold(); }
					_append_inline_markdown(cell);
					if (row == 0) { transcript->pop(); }
					transcript->pop();
				}
				int next = i + (row == 0 ? 2 : 1);
				if (row == 0) { i++; }
				if (next >= lines.size() || !lines[next].contains("|") || _agent_markdown_cells(lines[next]).size() != header.size()) { break; }
				i = next;
				row++;
			}
			transcript->pop();
			transcript->add_text("\n");
			continue;
		}
		int heading = 0;
		while (heading < line.length() && line[heading] == '#') { heading++; }
		bool is_heading = heading > 0 && heading <= 6 && line.substr(heading, 1) == " ";
		if (is_heading) {
			transcript->push_bold();
			line = line.substr(heading + 1);
		} else if (line.begins_with("- ") || line.begins_with("* ") || line.begins_with("+ ")) {
			line = String::utf8("• ") + line.substr(2);
		} else if (line.begins_with("> ")) {
			line = String::utf8("│ ") + line.substr(2);
		}
		_append_inline_markdown(line);
		if (is_heading) { transcript->pop(); }
		transcript->add_text("\n");
	}
}

void AgentWorkspace::_append_message(const String &p_author, const String &p_text) {
	transcript->push_bold();
	transcript->add_text(p_author);
	transcript->pop();
	transcript->add_text("\n");
	_append_markdown(p_text);
	transcript->add_text("\n");
}

PackedStringArray AgentWorkspace::_get_dirty_files() const {
	PackedStringArray dirty;
	if (ScriptEditor::get_singleton()) {
		PackedStringArray unsaved = ScriptEditor::get_singleton()->get_unsaved_scripts();
		Vector<Ref<Script>> open_scripts = ScriptEditor::get_singleton()->get_open_scripts();
		for (const String &name : unsaved) {
			bool found = false;
			for (const Ref<Script> &script : open_scripts) {
				if (script.is_valid() && (script->get_path() == name || script->get_path().get_file() == name)) {
					dirty.push_back(script->get_path());
					found = true;
					break;
				}
			}
			if (!found) {
				dirty.push_back(name);
			}
		}
	}
	EditorData &editor_data = EditorNode::get_editor_data();
	for (int index = 0; index < editor_data.get_edited_scene_count(); index++) {
		if (editor_data.is_scene_changed(index)) {
			dirty.push_back(editor_data.get_scene_path(index));
		}
	}
	return dirty;
}

void AgentWorkspace::_scan_changes() {
	if (EditorFileSystem::get_singleton()) {
		EditorFileSystem::get_singleton()->scan_changes();
	}
}

void AgentWorkspace::_request(const String &p_path, HTTPClient::Method p_method, const Dictionary &p_body, RequestKind p_kind) {
	if (request_kind != REQUEST_NONE || backend_url.is_empty()) {
		return;
	}
	PackedStringArray headers;
	headers.push_back("Authorization: Bearer " + backend_token);
	headers.push_back("Content-Type: application/json; charset=utf-8");
	String body = p_body.is_empty() ? String() : JSON::stringify(p_body);
	Error error = http->request(backend_url + p_path, headers, p_method, body);
	if (error != OK) {
		_set_status(vformat(_u8("无法连接 Agent 后台（%d）"), error), true);
		return;
	}
	request_kind = p_kind;
}

void AgentWorkspace::_push_configuration(bool p_include_key) {
	Dictionary body;
	body["api_key"] = p_include_key ? api_key->get_text() : String();
	body["base_url"] = base_url->get_text().strip_edges();
	body["model"] = model->get_text().strip_edges();
	body["vision_model"] = vision_model->get_text().strip_edges();
	body["response_language"] = response_language->get_item_metadata(response_language->get_selected());
	_request("/v1/configure", HTTPClient::METHOD_POST, body, REQUEST_CONFIGURE);
}

void AgentWorkspace::_request_completed(int p_result, int p_code, const PackedStringArray &p_headers, const PackedByteArray &p_body) {
	RequestKind completed = request_kind;
	request_kind = REQUEST_NONE;
	String response_text = String::utf8((const char *)p_body.ptr(), p_body.size());
	Variant parsed = JSON::parse_string(response_text);
	Dictionary response = parsed.get_type() == Variant::DICTIONARY ? Dictionary(parsed) : Dictionary();
	if (p_result != HTTPRequest::RESULT_SUCCESS || p_code < 200 || p_code >= 300) {
		String detail = response.has("detail") ? String(response["detail"]) : response_text;
		_set_status(vformat(_u8("后台请求失败（HTTP %d）：%s"), p_code, detail), true);
		return;
	}

	switch (completed) {
		case REQUEST_HEALTH: {
			if ((int)response.get("protocol_version", 0) != 1 || String(response.get("project", "")).simplify_path() != ProjectSettings::get_singleton()->get_resource_path().simplify_path()) {
				_set_status(_u8("Agent 后台握手不兼容或项目绑定错误"), true);
				return;
			}
			backend_ready = true;
			last_agent_run_id = response.get("last_revertible_run_id", "");
			_set_status(_u8("Agent 已连接"));
			_push_configuration(false);
		} break;
		case REQUEST_CONFIGURE: {
			api_key->clear();
			api_key->set_placeholder(response.get("has_api_key", false) ? _u8("已保存到 Windows 凭据管理器") : _u8("输入 API Key"));
			_request("/v1/chat/history", HTTPClient::METHOD_GET, Dictionary(), REQUEST_HISTORY);
		} break;
		case REQUEST_HISTORY: {
			transcript->clear();
			Array messages = response.get("messages", Array());
			for (int index = 0; index < messages.size(); index++) {
				Dictionary message = messages[index];
				String role = message.get("role", "assistant");
				String content = message.get("content", "");
				Array attachments = message.get("attachments", Array());
				if (!attachments.is_empty()) {
					content += _u8("\n\n附件：");
					for (int attachment_index = 0; attachment_index < attachments.size(); attachment_index++) {
						Dictionary attachment = attachments[attachment_index];
						content += (attachment_index == 0 ? " " : ", ") + String(attachment.get("name", attachment.get("project_path", "")));
					}
				}
				Array steps = message.get("steps", Array());
				for (int step_index = 0; step_index < MIN(steps.size(), 20); step_index++) {
					Dictionary step = steps[step_index];
					String description = step.get("description", "");
					if (!description.is_empty()) {
						content += "\n- " + description;
					}
				}
				_append_message(role == "user" ? _u8("你") : String("Agent"), content);
			}
			_load_harness_plan();
		} break;
		case REQUEST_CLEAR: {
			transcript->clear();
			_set_status(_u8("当前项目的对话历史已清空"));
		} break;
		case REQUEST_CANCEL: {
			_set_status(_u8("取消请求已发送"));
		} break;
		case REQUEST_ATTACHMENTS: {
			Array values = response.get("attachments", Array());
			for (int index = 0; index < values.size(); index++) {
				Dictionary item = values[index];
				String id = item.get("id", "");
				if (!id.is_empty() && !attachment_ids.has(id)) {
					attachment_ids.push_back(id);
				}
			}
			attachments_label->set_text(vformat(_u8("已添加 %d 个参考附件"), attachment_ids.size()));
		} break;
		case REQUEST_ASSETS: {
			Array values = response.get("assets", Array());
			_scan_changes();
			_set_status(vformat(_u8("已导入 %d 个素材到 res://assets"), values.size()));
		} break;
		case REQUEST_HARNESS_PLAN:
		case REQUEST_HARNESS_SAVE: {
			_render_harness_plan(response);
			if (completed == REQUEST_HARNESS_SAVE) {
				_set_status(_u8("验收计划已保存"));
			}
		} break;
		case REQUEST_ROLLBACK: {
			last_agent_run_id = response.get("last_revertible_run_id", "");
			_scan_changes();
			Array values = response.get("changed_files", Array());
			_set_status(vformat(_u8("已回退 %d 个文件"), values.size()));
		} break;
		default:
			break;
	}
}

void AgentWorkspace::_start_stream(const String &p_path, const Dictionary &p_body, StreamKind p_kind) {
	if (!backend_ready || task_running || stream_kind != STREAM_NONE) {
		return;
	}
	stream_client = Ref<HTTPClient>(HTTPClient::create());
	stream_client->set_read_chunk_size(8192);
	Error error = stream_client->connect_to_host("127.0.0.1", backend_url.get_slice(":", 2).to_int());
	if (error != OK) {
		_set_status(_u8("无法建立 SSE 连接"), true);
		stream_client.unref();
		return;
	}
	stream_path = p_path;
	stream_body = JSON::stringify(p_body);
	stream_kind = p_kind;
	stream_request_sent = false;
	stream_bytes.clear();
	stream_idle = 0.0;
	task_running = true;
	_set_test_busy(true);
	send_button->set_disabled(true);
	cancel_button->set_disabled(false);
	retry_button->set_disabled(true);
	_set_status(p_kind == STREAM_AGENT ? _u8("Agent 正在执行…") : _u8("正在运行验收测试…"));
}

void AgentWorkspace::_process_stream(double p_delta) {
	if (stream_kind == STREAM_NONE || stream_client.is_null()) {
		return;
	}
	stream_idle += p_delta;
	Error poll_error = stream_client->poll();
	if (poll_error != OK) {
		_finish_stream(true, _u8("SSE 连接中断"));
		return;
	}
	HTTPClient::Status client_status = stream_client->get_status();
	if (client_status == HTTPClient::STATUS_CONNECTED && !stream_request_sent) {
		Vector<String> headers;
		headers.push_back("Authorization: Bearer " + backend_token);
		headers.push_back("Content-Type: application/json; charset=utf-8");
		headers.push_back("Accept: text/event-stream");
		PackedByteArray bytes = stream_body.to_utf8_buffer();
		Error request_error = stream_client->request(HTTPClient::METHOD_POST, stream_path, headers, bytes.ptr(), bytes.size());
		if (request_error != OK) {
			_finish_stream(true, _u8("无法发送 SSE 请求"));
			return;
		}
		stream_request_sent = true;
		stream_idle = 0.0;
		return;
	}
	if (client_status == HTTPClient::STATUS_BODY) {
		PackedByteArray chunk = stream_client->read_response_body_chunk();
		if (!chunk.is_empty()) {
			stream_bytes.append_array(chunk);
			stream_idle = 0.0;
			_consume_stream_events();
		}
		return;
	}
	if (client_status == HTTPClient::STATUS_CANT_CONNECT || client_status == HTTPClient::STATUS_CANT_RESOLVE || client_status == HTTPClient::STATUS_CONNECTION_ERROR || client_status == HTTPClient::STATUS_TLS_HANDSHAKE_ERROR) {
		_finish_stream(true, _u8("SSE 后台连接失败"));
		return;
	}
	if (stream_request_sent && client_status == HTTPClient::STATUS_CONNECTED && task_running) {
		_finish_stream(true, _u8("SSE 响应提前结束"));
		return;
	}
	if (stream_idle > 30.0) {
		_finish_stream(true, _u8("SSE 事件超时"));
	}
}

void AgentWorkspace::_consume_stream_events() {
	while (true) {
		int separator = -1;
		for (int index = 0; index + 1 < stream_bytes.size(); index++) {
			if (stream_bytes[index] == '\n' && stream_bytes[index + 1] == '\n') {
				separator = index;
				break;
			}
		}
		if (separator < 0) {
			return;
		}
		PackedByteArray event_bytes;
		event_bytes.resize(separator);
		for (int index = 0; index < separator; index++) {
			event_bytes.write[index] = stream_bytes[index];
		}
		PackedByteArray remaining;
		int remaining_size = stream_bytes.size() - separator - 2;
		remaining.resize(remaining_size);
		for (int index = 0; index < remaining_size; index++) {
			remaining.write[index] = stream_bytes[separator + 2 + index];
		}
		stream_bytes = remaining;
		_handle_sse(String::utf8((const char *)event_bytes.ptr(), event_bytes.size()));
		if (stream_kind == STREAM_NONE) {
			return;
		}
	}
}

void AgentWorkspace::_handle_sse(const String &p_event) {
	String event_name;
	String data_text;
	PackedStringArray lines = p_event.replace("\r\n", "\n").split("\n");
	for (const String &line : lines) {
		if (line.begins_with("event:")) {
			event_name = line.trim_prefix("event:").strip_edges();
		} else if (line.begins_with("data:")) {
			data_text += line.trim_prefix("data:").strip_edges();
		}
	}
	Variant parsed = JSON::parse_string(data_text);
	if (parsed.get_type() != Variant::DICTIONARY) {
		return;
	}
	Dictionary event_data = parsed;
	if ((int)event_data.get("protocol_version", 0) != 1) {
		_finish_stream(true, _u8("收到不兼容的 SSE 协议事件"));
		return;
	}
	current_run_id = event_data.get("run_id", current_run_id);
	if (event_name == "editor_state_required") {
		Dictionary body;
		body["run_id"] = current_run_id;
		body["nonce"] = event_data.get("nonce", "");
		body["dirty_files"] = _get_dirty_files();
		PackedStringArray headers;
		headers.push_back("Authorization: Bearer " + backend_token);
		headers.push_back("Content-Type: application/json");
		editor_state_http->cancel_request();
		editor_state_http->request(backend_url + "/v1/editor/state", headers, HTTPClient::METHOD_POST, JSON::stringify(body));
		return;
	}
	if (event_name == "harness_phase") {
		String description = event_data.get("description", "");
		test_phase->set_text(description);
		_set_status(description);
		transcript->add_text(description + "\n");
		test_output->add_text(description + "\n");
		return;
	}
	if (event_name == "keepalive" || event_name == "protocol_version") {
		return;
	}
	if (event_name == "step") {
		String type = event_data.get("type", "step");
		String description = event_data.get("description", "");
		if (type == "files_changed") {
			_scan_changes();
		}
		if (!description.is_empty()) {
			transcript->add_text(String::utf8("• ") + description + "\n");
		}
		return;
	}
	if (event_name == "acceptance_plan") {
		Dictionary plan = event_data.get("plan", Dictionary());
		if (!plan.is_empty()) {
			_render_harness_plan(plan);
		}
		return;
	}
	if (event_name == "harness_observation") {
		test_phase->set_text(event_data.get("description", ""));
		test_output->add_text(String::utf8("• ") + String(event_data.get("description", "")) + "\n");
		return;
	}
	if (event_name == "harness_result") {
		String line = vformat(_u8("[%s] %s"), event_data.get("status", ""), event_data.get("failure", ""));
		Array artifacts = event_data.get("artifacts", Array());
		if (!artifacts.is_empty()) {
			line += _u8("  证据：") + String(artifacts[0]);
		}
		test_output->add_text(line + "\n");
		Array cases = test_plan.get("cases", Array());
		for (int index = 0; index < cases.size(); index++) {
			Dictionary value = cases[index];
			if (value.get("id", "") == event_data.get("case_id", "")) {
				value["status"] = event_data.get("status", "");
				value["failure"] = event_data.get("failure", "");
				value["artifacts"] = event_data.get("artifacts", Array());
				value["result"] = event_data.get("result", Dictionary());
			}
		}
		_render_harness_plan(test_plan);
		return;
	}
	if (event_name == "done") {
		Array changed_files = event_data.get("changed_files", Array());
		if (!changed_files.is_empty()) {
			last_agent_run_id = current_run_id;
			_scan_changes();
		}
		String reply = event_data.get("reply", "");
		if (!reply.is_empty()) {
			_append_message("Agent", reply);
		}
		if (stream_kind == STREAM_AGENT) {
			Array changed_files = event_data.get("changed_files", Array());
			if (!changed_files.is_empty()) {
				last_agent_run_id = current_run_id;
			}
			attachment_ids.clear();
			attachments_label->set_text(_u8("未添加参考附件"));
			_scan_changes();
		}
		String completion_status = event_data.get("status", "done");
		Dictionary summary = event_data.get("harness", Dictionary());
		if (completion_status == "cancelled") {
			_finish_stream(false, _u8("任务已取消"));
		} else if (!summary.is_empty()) {
			bool acceptance_ok = summary.get("acceptance_ok", false);
			Dictionary preflight = summary.get("preflight", Dictionary());
			String message = vformat(_u8("脚本检查：%s；行为验收：%d 通过 / %d 失败 / %d 受阻；警告 %d；回修 %d/3；测试修订 %d/3"),
					preflight.get("status", ""), summary.get("passed", 0), summary.get("failed", 0), summary.get("blocked", 0), summary.get("warning_count", 0), summary.get("repair_round", 0), summary.get("plan_revision", 0));
			if (bool(summary.get("stale", false))) {
				message += _u8("；项目已修改，结果过期");
			} else if (!acceptance_ok && int(summary.get("failed", 0)) == 0 && int(summary.get("blocked", 0)) == 0) {
				message += _u8("；尚未完成整套验收");
			}
			test_phase->set_text(message);
			transcript->add_text(message + "\n");
			_finish_stream(!acceptance_ok, message);
		} else if (stream_kind == STREAM_AGENT) {
			bool validation_ok = event_data.get("validation_ok", false);
			_finish_stream(false, validation_ok ? _u8("任务完成，脚本检查通过；无文件修改，未触发行为测试") : _u8("任务完成；无文件修改，未触发行为测试"));
		} else {
			_finish_stream(false, _u8("测试计划已生成，可运行单项或全部测试"));
		}
		return;
	}
	if (event_name == "error") {
		Array changed_files = event_data.get("changed_files", Array());
		if (!changed_files.is_empty()) {
			last_agent_run_id = current_run_id;
			_scan_changes();
		}
		retry_button->set_disabled(!event_data.get("retryable", false));
		_finish_stream(true, event_data.get("message", _u8("Agent 执行失败")));
	}
}

void AgentWorkspace::_finish_stream(bool p_error, const String &p_message) {
	if (stream_client.is_valid()) {
		stream_client->close();
		stream_client.unref();
	}
	stream_kind = STREAM_NONE;
	stream_request_sent = false;
	stream_bytes.clear();
	task_running = false;
	_set_test_busy(false);
	send_button->set_disabled(false);
	cancel_button->set_disabled(true);
	if (!p_message.is_empty()) {
		_set_status(p_message, p_error);
	}
}

void AgentWorkspace::_send() {
	String message = prompt->get_text().strip_edges();
	if (message.is_empty() || !backend_ready || task_running) {
		return;
	}
	last_prompt = message;
	Dictionary body;
	body["prompt"] = message;
	body["dirty_files"] = _get_dirty_files();
	body["attachment_ids"] = attachment_ids;
	_append_message(_u8("你"), message);
	prompt->clear();
	_start_stream("/v1/agent/run-stream", body, STREAM_AGENT);
}

void AgentWorkspace::_retry() {
	if (last_prompt.is_empty() || task_running) {
		return;
	}
	Dictionary body;
	body["prompt"] = last_prompt;
	body["dirty_files"] = _get_dirty_files();
	body["attachment_ids"] = attachment_ids;
	body["retry_run_id"] = current_run_id;
	_start_stream("/v1/agent/run-stream", body, STREAM_AGENT);
}

void AgentWorkspace::_cancel() {
	if (!task_running || current_run_id.is_empty()) {
		return;
	}
	Dictionary body;
	body["run_id"] = current_run_id;
	_request("/v1/agent/cancel", HTTPClient::METHOD_POST, body, REQUEST_CANCEL);
}

void AgentWorkspace::_clear_conversation() {
	if (!backend_ready || task_running) {
		return;
	}
	_request("/v1/chat/clear", HTTPClient::METHOD_POST, Dictionary(), REQUEST_CLEAR);
}

void AgentWorkspace::_save_settings() {
	EditorSettings *settings = EditorSettings::get_singleton();
	settings->set_setting("vibe_agent/base_url", base_url->get_text().strip_edges());
	settings->set_setting("vibe_agent/model", model->get_text().strip_edges());
	settings->set_setting("vibe_agent/vision_model", vision_model->get_text().strip_edges());
	settings->set_setting("vibe_agent/response_language", response_language->get_item_metadata(response_language->get_selected()));
	settings->save();
	_push_configuration(true);
}

void AgentWorkspace::_quick_prompt(const String &p_text) {
	prompt->set_text(p_text);
	prompt->grab_focus();
}

void AgentWorkspace::_show_settings() {
	settings_dialog->popup_centered_clamped(Size2(480, 620) * EDSCALE, 0.85);
}

void AgentWorkspace::_rollback_last_task() {
	if (!backend_ready || task_running || last_agent_run_id.is_empty()) {
		_set_status(_u8("当前没有可回退的 Agent 修改"), true);
		return;
	}
	Dictionary body;
	body["run_id"] = last_agent_run_id;
	body["dirty_files"] = _get_dirty_files();
	_request("/v1/agent/rollback", HTTPClient::METHOD_POST, body, REQUEST_ROLLBACK);
}

void AgentWorkspace::_choose_attachments() {
	attachment_dialog->popup_file_dialog();
}

void AgentWorkspace::_choose_assets() {
	asset_dialog->popup_file_dialog();
}

void AgentWorkspace::_attachments_selected(const PackedStringArray &p_paths) {
	Dictionary body;
	body["paths"] = p_paths;
	_request("/v1/attachments", HTTPClient::METHOD_POST, body, REQUEST_ATTACHMENTS);
}

void AgentWorkspace::_assets_selected(const PackedStringArray &p_paths) {
	Dictionary body;
	body["paths"] = p_paths;
	_request("/v1/assets/import", HTTPClient::METHOD_POST, body, REQUEST_ASSETS);
}

void AgentWorkspace::_paste_clipboard_image() {
	Ref<Image> image = DisplayServer::get_singleton()->clipboard_get_image();
	if (image.is_null() || image->is_empty()) {
		_set_status(_u8("剪贴板中没有图片"), true);
		return;
	}
	String inbox = ProjectSettings::get_singleton()->get_resource_path().path_join(".godot/agent/inbox");
	DirAccess::make_dir_recursive_absolute(inbox);
	String path = inbox.path_join("clipboard-" + String::num_uint64(OS::get_singleton()->get_ticks_msec()) + ".png");
	Error error = image->save_png(path);
	if (error != OK) {
		_set_status(_u8("无法保存剪贴板图片"), true);
		return;
	}
	PackedStringArray paths;
	paths.push_back(path);
	_attachments_selected(paths);
}

void AgentWorkspace::_files_dropped(const PackedStringArray &p_files) {
	if (!workspace_visible || task_running) {
		return;
	}
	PackedStringArray images;
	for (const String &path : p_files) {
		String extension = path.get_extension().to_lower();
		if (extension == "png" || extension == "jpg" || extension == "jpeg" || extension == "webp" || extension == "gif") {
			images.push_back(path);
		}
	}
	if (!images.is_empty()) {
		_attachments_selected(images);
	}
}

void AgentWorkspace::_load_harness_plan() {
	if (backend_ready && request_kind == REQUEST_NONE) {
		_request("/v1/harness/plan", HTTPClient::METHOD_GET, Dictionary(), REQUEST_HARNESS_PLAN);
	}
}

void AgentWorkspace::_save_harness_plan() {
	if (task_running) {
		return;
	}
	Variant value = JSON::parse_string(test_plan_editor->get_text());
	if (value.get_type() != Variant::DICTIONARY) {
		_set_status(_u8("验收计划 JSON 格式无效"), true);
		return;
	}
	_request("/v1/harness/plan", HTTPClient::METHOD_PUT, Dictionary(value), REQUEST_HARNESS_SAVE);
}

void AgentWorkspace::_run_selected_test() {
	TreeItem *selected = test_tree->get_selected();
	if (!selected || task_running) {
		return;
	}
	Array ids;
	ids.push_back(selected->get_metadata(0));
	Dictionary body;
	body["case_ids"] = ids;
	body["dirty_files"] = _get_dirty_files();
	test_output->clear();
	_start_stream("/v1/harness/run-stream", body, STREAM_HARNESS);
}

void AgentWorkspace::_run_all_tests() {
	if (task_running) {
		return;
	}
	Dictionary body;
	body["case_ids"] = Array();
	body["dirty_files"] = _get_dirty_files();
	test_output->clear();
	_start_stream("/v1/harness/run-stream", body, STREAM_HARNESS);
}

void AgentWorkspace::_open_selected_artifact() {
	TreeItem *item = test_tree->get_selected();
	if (!item) {
		_set_status(_u8("请先选择一个验收目标"), true);
		return;
	}
	String relative = item->get_metadata(1);
	String root = ProjectSettings::get_singleton()->get_resource_path().path_join(".godot/agent/test_artifacts").simplify_path();
	String path = root.path_join(relative).simplify_path();
	if (relative.is_empty() || (!path.begins_with(root + "/") && path != root) || !FileAccess::exists(path)) {
		_set_status(_u8("选中验收目标没有可用证据"), true);
		return;
	}
	OS::get_singleton()->shell_show_in_file_manager(path);
}

void AgentWorkspace::_render_harness_plan(const Dictionary &p_plan) {
	String selected_id;
	if (test_tree->get_selected()) {
		selected_id = test_tree->get_selected()->get_metadata(0);
	}
	test_plan = p_plan;
	test_plan_editor->set_text(JSON::stringify(p_plan, "  "));
	test_tree->clear();
	TreeItem *root = test_tree->create_item();
	Array cases = p_plan.get("cases", Array());
	int passed_count = 0, failed_count = 0, blocked_count = 0, pending_count = 0, warning_count = 0;
	for (int index = 0; index < cases.size(); index++) {
		Dictionary value = cases[index];
		if (bool(value.get("enabled", true))) {
			String status = value.get("status", "pending");
			passed_count += status == "passed";
			failed_count += status == "failed";
			blocked_count += status == "blocked";
			pending_count += status != "passed" && status != "failed" && status != "blocked";
			Dictionary result = value.get("result", Dictionary());
			warning_count += int(result.get("warning_count", 0));
		}
		TreeItem *item = test_tree->create_item(root);
		item->set_metadata(0, value.get("id", ""));
		Array artifacts = value.get("artifacts", Array());
		item->set_metadata(1, artifacts.is_empty() ? String() : String(artifacts[0]));
		item->set_text(0, value.get("status", "pending"));
		item->set_text(1, value.get("title", _u8("未命名验收目标")));
		item->set_text(2, value.get("required", false) ? _u8("必选") : _u8("可选"));
		String tooltip = _u8("Given：") + String(value.get("given", "")) + _u8("\nWhen：") + String(value.get("when", "")) + _u8("\nThen：") + String(value.get("then", ""));
		if (!String(value.get("failure", "")).is_empty()) {
			tooltip += _u8("\n失败原因：") + String(value.get("failure", ""));
		}
		item->set_tooltip_text(1, tooltip);
		if (String(value.get("id", "")) == selected_id || (selected_id.is_empty() && index == 0)) {
			item->select(0);
		}
	}
	if (current_run_id.is_empty()) {
		test_phase->set_text(vformat(_u8("行为验收：%d 通过 / %d 失败 / %d 受阻 / %d 待复测；警告 %d；回修 %d/3；测试修订 %d/3"),
				passed_count, failed_count, blocked_count, pending_count, warning_count, p_plan.get("repair_round", 0), p_plan.get("plan_revision", 0)));
	}
	_show_test_details();
}

void AgentWorkspace::_show_test_details() {
	test_details->clear();
	test_screenshot->set_texture(Ref<Texture2D>());
	test_screenshot->hide();
	TreeItem *item = test_tree->get_selected();
	if (!item) {
		return;
	}
	Array cases = test_plan.get("cases", Array());
	for (int index = 0; index < cases.size(); index++) {
		Dictionary value = cases[index];
		if (value.get("id", "") != item->get_metadata(0)) {
			continue;
		}
		test_details->add_text(String(value.get("title", "")) + "\n" +
				_u8("前置：") + String(value.get("given", "")) + "\n" +
				_u8("操作：") + String(value.get("when", "")) + "\n" +
				_u8("预期：") + String(value.get("then", "")) + "\n" +
				_u8("结果：") + String(value.get("status", "pending")) + " " + String(value.get("failure", "")) + "\n" +
				_u8("关联修改：") + JSON::stringify(value.get("change_files", Array())) + "\n\n" + _u8("执行步骤：\n"));
		Array steps = value.get("steps", Array());
		Dictionary review = value.get("test_review", Dictionary());
		if (!review.is_empty()) {
			test_details->add_text(_u8("测试合理性审查：") + String(review.get("decision", "")) + "\n" + String(review.get("reason", "")) + "\n" + String(review.get("evidence", "")) + "\n");
		}
		Dictionary result = value.get("result", Dictionary());
		test_details->add_text(_u8("行为断言：") + String(result.get("behavior_status", "not_run")) + _u8("；视觉：") + String(result.get("visual_status", "not_run")) + _u8("；运行：") + String(result.get("runtime_status", "not_run")) + "\n");
		Array diagnostics = result.get("diagnostics", Array());
		for (int diagnostic_index = 0; diagnostic_index < diagnostics.size(); diagnostic_index++) {
			Dictionary diagnostic = diagnostics[diagnostic_index];
			test_details->add_text(_u8("运行诊断：") + String(diagnostic.get("status", "")) + " " + String(diagnostic.get("summary", "")) + "\n");
		}
		Array observations = result.get("observations", Array());
		for (int step_index = 0; step_index < steps.size(); step_index++) {
			Dictionary step = steps[step_index];
			String op = step.get("op", "");
			String line;
			if (op == "action" || op == "key") {
				line = bool(step.get("pressed", true)) ? _u8("按下 ") : _u8("释放 ");
				line += String(step.get(op == "action" ? "action" : "key", ""));
			} else if (op == "mouse_move") {
				line = _u8("移动鼠标到 ") + JSON::stringify(step.get("position", Array()));
			} else if (op == "mouse_button") {
				line = bool(step.get("pressed", true)) ? _u8("按下鼠标按钮 ") : _u8("释放鼠标按钮 ");
				line += itos(int(step.get("button", 1)));
			} else if (op == "wait") {
				line = vformat(_u8("等待 %d 帧"), step.get("frames", 1));
			} else if (op == "load_scene") {
				line = _u8("进入场景 ") + String(step.get("scene", ""));
			} else if (op == "visual") {
				line = _u8("截图并检查：") + String(step.get("criteria", ""));
			} else if (op == "screenshot") {
				line = _u8("采集游戏画面");
			} else {
				line = (op == "read" ? _u8("记录 ") : _u8("检查 ")) + String(step.get("node", "")) + "." + String(step.get("property", ""));
				if (op != "read") {
					String reference = step.get("reference", "");
					line += " " + String(step.get("compare", "eq")) + " " + (reference.is_empty() ? JSON::stringify(step.get("value", Variant())) : _u8("先前记录：") + reference);
				}
			}
			test_details->add_text(itos(step_index + 1) + ". " + line + "\n");
			if (step_index < observations.size()) {
				Dictionary observation = observations[step_index];
				if (observation.has("actual")) {
					test_details->add_text(_u8("   实际：") + JSON::stringify(observation["actual"]) + "\n");
				}
				Dictionary visual = observation.get("visual", Dictionary());
				if (!visual.is_empty()) {
					test_details->add_text(_u8("   视觉判断：") + String(visual.get("status", "")) + " " + String(visual.get("summary", "")) + "\n");
				}
			}
		}
		Array artifacts = value.get("artifacts", Array());
		String root = ProjectSettings::get_singleton()->get_resource_path().path_join(".godot/agent/test_artifacts").simplify_path();
		for (int artifact_index = 0; artifact_index < artifacts.size(); artifact_index++) {
			String path = root.path_join(String(artifacts[artifact_index])).simplify_path();
			if (path.begins_with(root + "/") && path.get_extension() == "png" && FileAccess::exists(path)) {
				Ref<Image> picture;
				picture.instantiate();
				if (picture->load(path) == OK) {
					test_screenshot->set_texture(ImageTexture::create_from_image(picture));
					test_screenshot->show();
					break;
				}
			}
		}
		break;
	}
}

void AgentWorkspace::_regenerate_tests() {
	if (task_running || !backend_ready) {
		return;
	}
	Dictionary body;
	body["regenerate"] = true;
	body["dirty_files"] = _get_dirty_files();
	test_output->clear();
	_start_stream("/v1/harness/run-stream", body, STREAM_HARNESS);
}

void AgentWorkspace::_toggle_test_json(bool p_visible) {
	test_plan_editor->set_visible(p_visible);
}

void AgentWorkspace::_set_test_busy(bool p_busy) {
	test_plan_editor->set_editable(!p_busy);
	for (Button *button : test_edit_buttons) {
		button->set_disabled(p_busy);
	}
	test_stop->set_disabled(!p_busy);
}

void AgentWorkspace::_start_sidecar() {
	String executable_dir = OS::get_singleton()->get_executable_path().get_base_dir();
	String executable = executable_dir.path_join("vibe_agent/godotvibe-agent/godotvibe-agent.exe");
	String script;
	if (!FileAccess::exists(executable)) {
		String repository = executable_dir.get_base_dir();
		String development_sidecar = repository.path_join(".build/sidecar-dist/godotvibe-agent/godotvibe-agent.exe");
		if (FileAccess::exists(development_sidecar)) {
			executable = development_sidecar;
		} else {
			executable = repository.path_join(".venv/Scripts/pythonw.exe");
			script = repository.path_join("vibe_tools/editor_sidecar.py");
		}
	}
	if (!FileAccess::exists(executable) || (!script.is_empty() && !FileAccess::exists(script))) {
		_set_status(_u8("缺少 godotvibe-agent.exe；请先运行 build_sidecar.ps1，或配置仓库 .venv"), true);
		return;
	}
	String project = ProjectSettings::get_singleton()->get_resource_path();
	String instance_dir = project.path_join(".godot/agent/instances");
	DirAccess::make_dir_recursive_absolute(instance_dir);
	handshake_file = instance_dir.path_join("handshake-" + String::num_int64(OS::get_singleton()->get_process_id()) + ".json");
	DirAccess::remove_absolute(handshake_file);

	List<String> arguments;
	if (!script.is_empty()) {
		arguments.push_back(script);
	}
	arguments.push_back("--project");
	arguments.push_back(project);
	arguments.push_back("--parent-pid");
	arguments.push_back(String::num_int64(OS::get_singleton()->get_process_id()));
	arguments.push_back("--handshake");
	arguments.push_back(handshake_file);
	arguments.push_back("--godot-exe");
	arguments.push_back(OS::get_singleton()->get_executable_path());
	Error error = OS::get_singleton()->create_process(executable, arguments, &sidecar_pid, false);
	if (error != OK) {
		_set_status(vformat(_u8("无法启动 Agent 后台（%d）"), error), true);
		return;
	}
	startup_elapsed = 0.0;
	backend_ready = false;
	backend_url.clear();
	backend_token.clear();
	_set_status(_u8("正在启动 Agent 后台…"));
}

void AgentWorkspace::_read_handshake() {
	Ref<FileAccess> file = FileAccess::open(handshake_file, FileAccess::READ);
	if (file.is_null()) {
		return;
	}
	Variant parsed = JSON::parse_string(file->get_as_text());
	if (parsed.get_type() != Variant::DICTIONARY) {
		_set_status(_u8("Agent 握手文件损坏"), true);
		return;
	}
	Dictionary handshake = parsed;
	int protocol = handshake.get("protocol_version", 0);
	int port = handshake.get("port", 0);
	OS::ProcessID reported_pid = (OS::ProcessID)(int64_t)handshake.get("pid", 0);
	String token = handshake.get("token", "");
	if (protocol != 1 || port < 1024 || port > 65535 || reported_pid != sidecar_pid || token.length() != 64) {
		_set_status(_u8("Agent 握手校验失败"), true);
		return;
	}
	backend_url = "http://127.0.0.1:" + String::num_int64(port);
	backend_token = token;
	_request("/v1/health", HTTPClient::METHOD_GET, Dictionary(), REQUEST_HEALTH);
}

void AgentWorkspace::_stop_sidecar() {
	if (stream_client.is_valid()) {
		stream_client->close();
		stream_client.unref();
	}
	if (request_kind != REQUEST_NONE) {
		http->cancel_request();
		request_kind = REQUEST_NONE;
	}
	if (backend_ready && sidecar_pid != 0 && OS::get_singleton()->is_process_running(sidecar_pid)) {
		Ref<HTTPClient> shutdown_client = Ref<HTTPClient>(HTTPClient::create());
		int port = backend_url.get_slice(":", 2).to_int();
		if (shutdown_client->connect_to_host("127.0.0.1", port) == OK) {
			uint64_t deadline = OS::get_singleton()->get_ticks_msec() + 500;
			while (OS::get_singleton()->get_ticks_msec() < deadline) {
				shutdown_client->poll();
				if (shutdown_client->get_status() == HTTPClient::STATUS_CONNECTED) {
					Vector<String> headers;
					headers.push_back("Authorization: Bearer " + backend_token);
					headers.push_back("Content-Length: 0");
					shutdown_client->request(HTTPClient::METHOD_POST, "/v1/shutdown", headers, nullptr, 0);
					break;
				}
				OS::get_singleton()->delay_usec(10000);
			}
		}
		uint64_t exit_deadline = OS::get_singleton()->get_ticks_msec() + 800;
		while (OS::get_singleton()->is_process_running(sidecar_pid) && OS::get_singleton()->get_ticks_msec() < exit_deadline) {
			shutdown_client->poll();
			OS::get_singleton()->delay_usec(10000);
		}
	}
	if (sidecar_pid != 0 && OS::get_singleton()->is_process_running(sidecar_pid)) {
		OS::get_singleton()->kill(sidecar_pid);
	}
	sidecar_pid = 0;
	backend_ready = false;
	backend_url.clear();
	backend_token.clear();
	if (!handshake_file.is_empty()) {
		DirAccess::remove_absolute(handshake_file);
	}
}

void AgentWorkspace::_restart_sidecar() {
	if (request_kind != REQUEST_NONE) {
		http->cancel_request();
		request_kind = REQUEST_NONE;
	}
	_finish_stream(false);
	_stop_sidecar();
	_start_sidecar();
}

void AgentWorkspace::_notification(int p_what) {
	if (p_what == NOTIFICATION_READY) {
		get_tree()->get_root()->connect("files_dropped", callable_mp(this, &AgentWorkspace::_files_dropped));
		_start_sidecar();
		set_process(true);
		callable_mp(EditorInterface::get_singleton(), &EditorInterface::set_main_screen_editor).call_deferred("Agent");
	} else if (p_what == NOTIFICATION_PROCESS) {
		double delta = get_process_delta_time();
		if (!backend_ready && sidecar_pid != 0) {
			startup_elapsed += delta;
			if (!OS::get_singleton()->is_process_running(sidecar_pid)) {
				_set_status(_u8("Agent 后台异常退出；可点击顶部重启后台"), true);
				sidecar_pid = 0;
			} else if (backend_url.is_empty() && FileAccess::exists(handshake_file)) {
				_read_handshake();
			} else if (startup_elapsed > 20.0) {
				_set_status(_u8("Agent 后台启动超时；请检查诊断日志"), true);
			}
		}
		_process_stream(delta);
		if (test_evidence && test_evidence->is_visible_in_tree()) {
			test_evidence->set_vertical(test_evidence->get_size().x < 600 * EDSCALE);
		}
		test_refresh_elapsed += delta;
		if (backend_ready && workspace_visible && !task_running && request_kind == REQUEST_NONE &&
				tabs->get_current_tab() == 1 && !test_plan_editor->is_visible() && test_refresh_elapsed > 3.0) {
			test_refresh_elapsed = 0.0;
			_load_harness_plan();
		}
	} else if (p_what == NOTIFICATION_EXIT_TREE) {
		if (get_tree() && get_tree()->get_root()->is_connected("files_dropped", callable_mp(this, &AgentWorkspace::_files_dropped))) {
			get_tree()->get_root()->disconnect("files_dropped", callable_mp(this, &AgentWorkspace::_files_dropped));
		}
		_stop_sidecar();
	}
}

AgentWorkspace::AgentWorkspace() {
	set_name("AgentWorkspace");
	set_v_size_flags(SIZE_EXPAND_FILL);
	set_h_size_flags(SIZE_EXPAND_FILL);
	set_custom_minimum_size(Size2(720, 480));

	HBoxContainer *header = memnew(HBoxContainer);
	add_child(header);
	Label *title = memnew(Label("Agent"));
	title->add_theme_font_size_override("font_size", 20 * EDSCALE);
	header->add_child(title);
	status = memnew(Label(_u8("尚未连接")));
	status->set_h_size_flags(SIZE_EXPAND_FILL);
	status->set_horizontal_alignment(HORIZONTAL_ALIGNMENT_RIGHT);
	header->add_child(status);
	Button *restart = memnew(Button(_u8("重启后台")));
	header->add_child(restart);
	restart->connect(SceneStringName(pressed), callable_mp(this, &AgentWorkspace::_restart_sidecar));

	Button *settings_button = memnew(Button(_u8("设置")));
	header->add_child(settings_button);
	settings_button->connect(SceneStringName(pressed), callable_mp(this, &AgentWorkspace::_show_settings));

	tabs = memnew(TabContainer);
	tabs->set_h_size_flags(SIZE_EXPAND_FILL);
	tabs->set_v_size_flags(SIZE_EXPAND_FILL);
	add_child(tabs);

	VBoxContainer *chat = memnew(VBoxContainer);
	chat->set_name("Chat");
	tabs->add_child(chat);
	tabs->set_tab_title(0, _u8("对话"));

	transcript = memnew(RichTextLabel);
	transcript->set_selection_enabled(true);
	transcript->set_scroll_follow(true);
	transcript->set_v_size_flags(SIZE_EXPAND_FILL);
	transcript->set_context_menu_enabled(true);
	transcript->connect("meta_clicked", callable_mp(this, &AgentWorkspace::_open_markdown_link));
	chat->add_child(transcript);

	HBoxContainer *quick = memnew(HBoxContainer);
	chat->add_child(quick);
	const char *quick_titles[] = { "检查项目", "实现功能", "修复错误", "创建场景" };
	const char *quick_prompts[] = {
		"检查当前项目的结构、脚本和运行错误，并修复发现的问题。",
		"请根据我的描述在当前项目中实现一个完整功能：",
		"分析当前项目的错误日志和相关文件，定位并修复问题。",
		"在当前项目中创建一个可运行的新场景：",
	};
	for (int index = 0; index < 4; index++) {
		Button *button = memnew(Button(_u8(quick_titles[index])));
		quick->add_child(button);
		button->connect(SceneStringName(pressed), callable_mp(this, &AgentWorkspace::_quick_prompt).bind(_u8(quick_prompts[index])));
	}

	HBoxContainer *attachments = memnew(HBoxContainer);
	chat->add_child(attachments);
	Button *attach = memnew(Button(_u8("添加参考图片")));
	attachments->add_child(attach);
	attach->connect(SceneStringName(pressed), callable_mp(this, &AgentWorkspace::_choose_attachments));
	Button *paste = memnew(Button(_u8("粘贴剪贴板图片")));
	attachments->add_child(paste);
	paste->connect(SceneStringName(pressed), callable_mp(this, &AgentWorkspace::_paste_clipboard_image));
	Button *import_assets = memnew(Button(_u8("导入游戏素材")));
	attachments->add_child(import_assets);
	import_assets->connect(SceneStringName(pressed), callable_mp(this, &AgentWorkspace::_choose_assets));
	attachments_label = memnew(Label(_u8("未添加参考附件")));
	attachments_label->set_h_size_flags(SIZE_EXPAND_FILL);
	attachments->add_child(attachments_label);

	prompt = memnew(TextEdit);
	prompt->set_custom_minimum_size(Size2(0, 100 * EDSCALE));
	prompt->set_placeholder(_u8("描述需要创建、修改或检查的内容。可拖入图片，也可从剪贴板粘贴。"));
	chat->add_child(prompt);

	HBoxContainer *actions = memnew(HBoxContainer);
	chat->add_child(actions);
	send_button = memnew(Button(_u8("发送")));
	send_button->set_h_size_flags(SIZE_EXPAND_FILL);
	actions->add_child(send_button);
	send_button->connect(SceneStringName(pressed), callable_mp(this, &AgentWorkspace::_send));
	cancel_button = memnew(Button(_u8("取消")));
	cancel_button->set_disabled(true);
	actions->add_child(cancel_button);
	cancel_button->connect(SceneStringName(pressed), callable_mp(this, &AgentWorkspace::_cancel));
	retry_button = memnew(Button(_u8("重试失败任务")));
	retry_button->set_disabled(true);
	actions->add_child(retry_button);
	retry_button->connect(SceneStringName(pressed), callable_mp(this, &AgentWorkspace::_retry));

	ScrollContainer *tests_scroll = memnew(ScrollContainer);
	tests_scroll->set_name("Tests");
	tests_scroll->set_horizontal_scroll_mode(ScrollContainer::SCROLL_MODE_DISABLED);
	tabs->add_child(tests_scroll);
	VBoxContainer *tests = memnew(VBoxContainer);
	tests->set_h_size_flags(SIZE_EXPAND_FILL);
	tests->set_v_size_flags(SIZE_EXPAND_FILL);
	tests_scroll->add_child(tests);
	tabs->set_tab_title(1, "Tests");
	HFlowContainer *test_actions = memnew(HFlowContainer);
	tests->add_child(test_actions);
	test_phase = memnew(Label(_u8("Agent 修改文件后会自动生成测试并运行，失败最多回修 3 轮")));
	test_phase->set_autowrap_mode(TextServer::AUTOWRAP_WORD_SMART);
	tests->add_child(test_phase);
	Button *refresh_plan = memnew(Button(_u8("刷新计划")));
	test_actions->add_child(refresh_plan);
	refresh_plan->connect(SceneStringName(pressed), callable_mp(this, &AgentWorkspace::_load_harness_plan));
	test_edit_buttons.push_back(refresh_plan);
	Button *save_plan = memnew(Button(_u8("保存计划")));
	test_actions->add_child(save_plan);
	save_plan->connect(SceneStringName(pressed), callable_mp(this, &AgentWorkspace::_save_harness_plan));
	test_edit_buttons.push_back(save_plan);
	Button *regenerate = memnew(Button(_u8("重新生成")));
	test_actions->add_child(regenerate);
	regenerate->connect(SceneStringName(pressed), callable_mp(this, &AgentWorkspace::_regenerate_tests));
	test_edit_buttons.push_back(regenerate);
	Button *run_selected = memnew(Button(_u8("运行选中项")));
	test_actions->add_child(run_selected);
	run_selected->connect(SceneStringName(pressed), callable_mp(this, &AgentWorkspace::_run_selected_test));
	test_edit_buttons.push_back(run_selected);
	Button *run_all = memnew(Button(_u8("运行全部")));
	test_actions->add_child(run_all);
	run_all->connect(SceneStringName(pressed), callable_mp(this, &AgentWorkspace::_run_all_tests));
	test_edit_buttons.push_back(run_all);
	test_stop = memnew(Button(_u8("停止")));
	test_stop->set_disabled(true);
	test_actions->add_child(test_stop);
	test_stop->connect(SceneStringName(pressed), callable_mp(this, &AgentWorkspace::_cancel));
	Button *open_artifact = memnew(Button(_u8("定位选中证据")));
	test_actions->add_child(open_artifact);
	open_artifact->connect(SceneStringName(pressed), callable_mp(this, &AgentWorkspace::_open_selected_artifact));

	test_tree = memnew(Tree);
	test_tree->set_columns(3);
	test_tree->set_column_title(0, _u8("状态"));
	test_tree->set_column_title(1, _u8("验收目标"));
	test_tree->set_column_title(2, _u8("级别"));
	test_tree->set_column_titles_visible(true);
	test_tree->set_custom_minimum_size(Size2(0, 150 * EDSCALE));
	tests->add_child(test_tree);
	test_tree->connect("item_selected", callable_mp(this, &AgentWorkspace::_show_test_details));
	test_evidence = memnew(BoxContainer);
	test_evidence->set_v_size_flags(SIZE_EXPAND_FILL);
	tests->add_child(test_evidence);
	test_details = memnew(RichTextLabel);
	test_details->set_selection_enabled(true);
	test_details->set_custom_minimum_size(Size2(0, 180 * EDSCALE));
	test_details->set_h_size_flags(SIZE_EXPAND_FILL);
	test_evidence->add_child(test_details);
	test_screenshot = memnew(TextureRect);
	test_screenshot->set_expand_mode(TextureRect::EXPAND_IGNORE_SIZE);
	test_screenshot->set_stretch_mode(TextureRect::STRETCH_KEEP_ASPECT_CENTERED);
	test_screenshot->set_custom_minimum_size(Size2(0, 180 * EDSCALE));
	test_screenshot->set_h_size_flags(SIZE_EXPAND_FILL);
	test_evidence->add_child(test_screenshot);
	test_screenshot->hide();
	Button *advanced = memnew(Button(_u8("高级：编辑测试计划 JSON")));
	advanced->set_toggle_mode(true);
	advanced->connect(SceneStringName(toggled), callable_mp(this, &AgentWorkspace::_toggle_test_json));
	tests->add_child(advanced);
	test_plan_editor = memnew(TextEdit);
	test_plan_editor->set_v_size_flags(SIZE_EXPAND_FILL);
	test_plan_editor->set_placeholder(_u8("验收计划 JSON：可编辑 Given / When / Then、启用和必选状态。"));
	tests->add_child(test_plan_editor);
	test_plan_editor->hide();
	test_plan_editor->set_custom_minimum_size(Size2(0, 150 * EDSCALE));
	test_output = memnew(RichTextLabel);
	test_output->set_selection_enabled(true);
	test_output->set_custom_minimum_size(Size2(0, 110 * EDSCALE));
	tests->add_child(test_output);
	editor_state_http = memnew(HTTPRequest);
	editor_state_http->set_timeout(8);
	add_child(editor_state_http);

	settings_dialog = memnew(AcceptDialog);
	settings_dialog->set_title(_u8("Agent 设置"));
	settings_dialog->set_ok_button_text(_u8("关闭"));
	settings_dialog->set_flag(Window::FLAG_RESIZE_DISABLED, false);
	add_child(settings_dialog);
	ScrollContainer *settings_scroll = memnew(ScrollContainer);
	settings_scroll->set_horizontal_scroll_mode(ScrollContainer::SCROLL_MODE_DISABLED);
	settings_dialog->add_child(settings_scroll);
	VBoxContainer *settings_content = memnew(VBoxContainer);
	settings_content->set_h_size_flags(SIZE_EXPAND_FILL);
	settings_scroll->add_child(settings_content);
	GridContainer *settings_grid = memnew(GridContainer);
	settings_grid->set_columns(1);
	settings_grid->set_h_size_flags(SIZE_EXPAND_FILL);
	settings_content->add_child(settings_grid);
	auto add_field = [settings_grid](const String &p_label, LineEdit **r_field) {
		settings_grid->add_child(memnew(Label(p_label)));
		*r_field = memnew(LineEdit);
		(*r_field)->set_h_size_flags(SIZE_EXPAND_FILL);
		settings_grid->add_child(*r_field);
	};
	add_field("API Key", &api_key);
	api_key->set_secret(true);
	add_field(_u8("接口地址"), &base_url);
	add_field(_u8("主模型"), &model);
	add_field(_u8("视觉模型"), &vision_model);
	base_url->set_text(EDITOR_DEF("vibe_agent/base_url", ""));
	model->set_text(EDITOR_DEF("vibe_agent/model", "claude-sonnet-4-20250514"));
	vision_model->set_text(EDITOR_DEF("vibe_agent/vision_model", "gemini-2.5-flash"));
	Label *language_label = memnew(Label(_u8("回答语言")));
	settings_grid->add_child(language_label);
	response_language = memnew(OptionButton);
	response_language->add_item(_u8("简体中文"));
	response_language->set_item_metadata(0, "zh-CN");
	response_language->add_item("English");
	response_language->set_item_metadata(1, "en");
	response_language->add_item(_u8("跟随用户输入"));
	response_language->set_item_metadata(2, "auto");
	String saved_language = EDITOR_DEF("vibe_agent/response_language", "zh-CN");
	response_language->select(saved_language == "en" ? 1 : saved_language == "auto" ? 2 : 0);
	settings_grid->add_child(response_language);
	Button *save_settings = memnew(Button(_u8("保存设置")));
	settings_content->add_child(save_settings);
	save_settings->connect(SceneStringName(pressed), callable_mp(this, &AgentWorkspace::_save_settings));
	Button *clear_history = memnew(Button(_u8("清空当前项目对话")));
	settings_content->add_child(clear_history);
	clear_history->connect(SceneStringName(pressed), callable_mp(this, &AgentWorkspace::_clear_conversation));
	Button *rollback_task = memnew(Button(_u8("回退上次 Agent 修改")));
	settings_content->add_child(rollback_task);
	rollback_task->connect(SceneStringName(pressed), callable_mp(this, &AgentWorkspace::_rollback_last_task));

	http = memnew(HTTPRequest);
	add_child(http);
	http->set_timeout(20.0);
	http->connect("request_completed", callable_mp(this, &AgentWorkspace::_request_completed));

	attachment_dialog = memnew(EditorFileDialog);
	attachment_dialog->set_access(EditorFileDialog::ACCESS_FILESYSTEM);
	attachment_dialog->set_file_mode(EditorFileDialog::FILE_MODE_OPEN_FILES);
	attachment_dialog->add_filter("*.png,*.jpg,*.jpeg,*.webp,*.gif", _u8("参考图片"));
	add_child(attachment_dialog);
	attachment_dialog->connect("files_selected", callable_mp(this, &AgentWorkspace::_attachments_selected));
	asset_dialog = memnew(EditorFileDialog);
	asset_dialog->set_access(EditorFileDialog::ACCESS_FILESYSTEM);
	asset_dialog->set_file_mode(EditorFileDialog::FILE_MODE_OPEN_FILES);
	asset_dialog->add_filter("*.png,*.jpg,*.jpeg,*.webp,*.gif,*.bmp,*.tga,*.svg,*.wav,*.ogg,*.mp3,*.glb,*.gltf,*.obj,*.ttf,*.otf", _u8("游戏素材"));
	add_child(asset_dialog);
	asset_dialog->connect("files_selected", callable_mp(this, &AgentWorkspace::_assets_selected));
}

AgentWorkspace::~AgentWorkspace() {
	_stop_sidecar();
}

void AgentEditorPlugin::make_visible(bool p_visible) {
	workspace->workspace_visible = p_visible;
	workspace->set_visible(p_visible);
}

void AgentEditorPlugin::selected_notify() {
	workspace->prompt->grab_focus();
}

AgentEditorPlugin::AgentEditorPlugin() {
	workspace = memnew(AgentWorkspace);
	EditorNode::get_singleton()->get_editor_main_screen()->get_control()->add_child(workspace);
	workspace->set_anchors_and_offsets_preset(Control::PRESET_FULL_RECT);
	workspace->hide();
}
