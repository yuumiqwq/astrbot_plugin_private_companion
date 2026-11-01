# AstrBot shared conversation integration

Private sessions configured with AstrBot's `conversation_manager.shared_history` use the host conversation when composing proactive contacts. The integration calls `get_curr_conversation_id`, `get_conversation`, and `prepare_session_request`, so QQ synchronization and context compression follow the same host policy as ordinary replies. It does not make a separate QQ history request or apply the companion plugin's dialogue count and character limits to the imported host history.

The host verifies the selected role and supplies its current persona instruction, including processed preset examples. Ordinary dialogue from the plugin's observation store is replaced by the prepared host history. Delivery candidates remain available for duplicate detection. Hosts without the shared conversation interface retain the upstream behavior. Administrator diagnostics in a configured shared private session also use the current host persona and history, so their output can validate the same context integration as regular contacts.

Preparation happens before the model request budget check. If the prepared history exceeds the selected model profile, the contact fails with the existing size error instead of silently omitting the shared conversation. Persona self-profile overrides and proactive scheduling settings retain their existing behavior.

For the same shared private sessions, proactive text also follows AstrBot's enabled regular-expression segmentation setting, while the companion plugin's own style rewriting and segmentation features remain disabled. A newline rule separates each nonempty line into a delivery, including single newlines and blank-line boundaries; short sentences on the same line remain together.
