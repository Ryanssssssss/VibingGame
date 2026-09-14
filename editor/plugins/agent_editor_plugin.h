/**************************************************************************/
/*  agent_editor_plugin.h                                                 */
/**************************************************************************/

#pragma once

#include "core/io/http_client.h"
#include "core/os/os.h"
#include "editor/plugins/editor_plugin.h"
#include "scene/gui/box_container.h"

class Button;
class AcceptDialog;
class EditorFileDialog;
class HTTPRequest;
class Label;
class LineEdit;
class OptionButton;
class RichTextLabel;
class TabContainer;
class TextEdit;
class Tree;
class TextureRect;

class AgentWorkspace : public VBoxContainer {
	GDCLASS(AgentWorkspace, VBoxContainer);
	friend class AgentEditorPlugin;

	enum RequestKind {
		REQUEST_NONE,
		REQUEST_HEALTH,
		REQUEST_CONFIGURE,
		REQUEST_HISTORY,
		REQUEST_CLEAR,
		REQUEST_CANCEL,
		REQUEST_ATTACHMENTS,
		REQUEST_ASSETS,
		REQUEST_HARNESS_PLAN,
		REQUEST_HARNESS_SAVE,
		REQUEST_ROLLBACK,
	};

	enum StreamKind {
		STREAM_NONE,
		STREAM_AGENT,
		STREAM_HARNESS,
	};

	HTTPRequest *http = nullptr;
	HTTPRequest *editor_state_http = nullptr;
	Ref<HTTPClient> stream_client;
	PackedByteArray stream_bytes;
	String stream_path;
	String stream_body;
	StreamKind stream_kind = STREAM_NONE;
	bool stream_request_sent = false;
	double stream_idle = 0.0;

	TabContainer *tabs = nullptr;
	RichTextLabel *transcript = nullptr;
	TextEdit *prompt = nullptr;
	Label *status = nullptr;
	Label *attachments_label = nullptr;
	LineEdit *api_key = nullptr;
	LineEdit *base_url = nullptr;
	LineEdit *model = nullptr;
	LineEdit *vision_model = nullptr;
	OptionButton *response_language = nullptr;
	AcceptDialog *settings_dialog = nullptr;
	Button *send_button = nullptr;
	Button *cancel_button = nullptr;
	Button *retry_button = nullptr;
	Tree *test_tree = nullptr;
	TextEdit *test_plan_editor = nullptr;
	RichTextLabel *test_output = nullptr;
	RichTextLabel *test_details = nullptr;
	BoxContainer *test_evidence = nullptr;
	TextureRect *test_screenshot = nullptr;
	Label *test_phase = nullptr;
	Button *test_stop = nullptr;
	Vector<Button *> test_edit_buttons;
	Dictionary test_plan;
	double test_refresh_elapsed = 0.0;
	EditorFileDialog *attachment_dialog = nullptr;
	EditorFileDialog *asset_dialog = nullptr;

	OS::ProcessID sidecar_pid = 0;
	String handshake_file;
	String backend_url;
	String backend_token;
	String current_run_id;
	String last_agent_run_id;
	String last_prompt;
	PackedStringArray attachment_ids;
	RequestKind request_kind = REQUEST_NONE;
	bool backend_ready = false;
	bool task_running = false;
	bool workspace_visible = false;
	double startup_elapsed = 0.0;

	static String _u8(const char *p_text);
	void _set_status(const String &p_text, bool p_error = false);
	void _append_inline_markdown(const String &p_text, int p_depth = 0);
	void _open_markdown_link(const Variant &p_meta);
	void _append_markdown(const String &p_text);
	void _append_message(const String &p_author, const String &p_text);
	PackedStringArray _get_dirty_files() const;
	void _scan_changes();

	void _start_sidecar();
	void _stop_sidecar();
	void _restart_sidecar();
	void _read_handshake();
	void _request(const String &p_path, HTTPClient::Method p_method, const Dictionary &p_body, RequestKind p_kind);
	void _request_completed(int p_result, int p_code, const PackedStringArray &p_headers, const PackedByteArray &p_body);
	void _push_configuration(bool p_include_key);

	void _start_stream(const String &p_path, const Dictionary &p_body, StreamKind p_kind);
	void _process_stream(double p_delta);
	void _consume_stream_events();
	void _handle_sse(const String &p_event);
	void _finish_stream(bool p_error, const String &p_message = String());

	void _send();
	void _retry();
	void _cancel();
	void _clear_conversation();
	void _save_settings();
	void _show_settings();
	void _quick_prompt(const String &p_text);
	void _rollback_last_task();

	void _choose_attachments();
	void _choose_assets();
	void _attachments_selected(const PackedStringArray &p_paths);
	void _assets_selected(const PackedStringArray &p_paths);
	void _paste_clipboard_image();
	void _files_dropped(const PackedStringArray &p_files);

	void _load_harness_plan();
	void _save_harness_plan();
	void _run_selected_test();
	void _run_all_tests();
	void _open_selected_artifact();
	void _render_harness_plan(const Dictionary &p_plan);
	void _show_test_details();
	void _regenerate_tests();
	void _toggle_test_json(bool p_visible);
	void _set_test_busy(bool p_busy);

protected:
	void _notification(int p_what);

public:
	AgentWorkspace();
	~AgentWorkspace();
};

class AgentEditorPlugin : public EditorPlugin {
	GDCLASS(AgentEditorPlugin, EditorPlugin);

	AgentWorkspace *workspace = nullptr;

public:
	virtual String get_plugin_name() const override { return "Agent"; }
	virtual bool has_main_screen() const override { return true; }
	virtual void edit(Object *p_object) override {}
	virtual bool handles(Object *p_object) const override { return false; }
	virtual void make_visible(bool p_visible) override;
	virtual void selected_notify() override;

	AgentEditorPlugin();
};
