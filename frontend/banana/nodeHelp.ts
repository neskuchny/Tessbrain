// Справка по типам блоков доски: что делает, что подключать, пример.
// Показывается по клику на «?» в шапке блока (BaseNode).
// Значения — ключи i18n (namespace: banana_node_help), резолвятся в BaseNode через useTranslations.

export interface NodeHelp {
  whatKey: string
  connectKey: string
  exampleKey: string
}

export const NODE_HELP: Record<string, NodeHelp> = {
  // ── Креатив ──
  imageInput: {
    whatKey: 'image_input_what',
    connectKey: 'image_input_connect',
    exampleKey: 'image_input_example',
  },
  annotation: {
    whatKey: 'annotation_what',
    connectKey: 'annotation_connect',
    exampleKey: 'annotation_example',
  },
  prompt: {
    whatKey: 'prompt_what',
    connectKey: 'prompt_connect',
    exampleKey: 'prompt_example',
  },
  textCombiner: {
    whatKey: 'text_combiner_what',
    connectKey: 'text_combiner_connect',
    exampleKey: 'text_combiner_example',
  },
  nanoBanana: {
    whatKey: 'nano_banana_what',
    connectKey: 'nano_banana_connect',
    exampleKey: 'nano_banana_example',
  },
  llmGenerate: {
    whatKey: 'llm_generate_what',
    connectKey: 'llm_generate_connect',
    exampleKey: 'llm_generate_example',
  },
  infographic: {
    whatKey: 'infographic_what',
    connectKey: 'infographic_connect',
    exampleKey: 'infographic_example',
  },
  output: {
    whatKey: 'output_what',
    connectKey: 'output_connect',
    exampleKey: 'output_example',
  },
  // ── Процессы ──
  trigger: {
    whatKey: 'trigger_what',
    connectKey: 'trigger_connect',
    exampleKey: 'trigger_example',
  },
  ask_brain: {
    whatKey: 'ask_brain_what',
    connectKey: 'ask_brain_connect',
    exampleKey: 'ask_brain_example',
  },
  report: {
    whatKey: 'report_what',
    connectKey: 'report_connect',
    exampleKey: 'report_example',
  },
  notify: {
    whatKey: 'notify_what',
    connectKey: 'notify_connect',
    exampleKey: 'notify_example',
  },
  task: {
    whatKey: 'task_what',
    connectKey: 'task_connect',
    exampleKey: 'task_example',
  },
  condition: {
    whatKey: 'condition_what',
    connectKey: 'condition_connect',
    exampleKey: 'condition_example',
  },
  wait_reply: {
    whatKey: 'wait_reply_what',
    connectKey: 'wait_reply_connect',
    exampleKey: 'wait_reply_example',
  },
  meeting_data: {
    whatKey: 'meeting_data_what',
    connectKey: 'meeting_data_connect',
    exampleKey: 'meeting_data_example',
  },
  meeting_share: {
    whatKey: 'meeting_share_what',
    connectKey: 'meeting_share_connect',
    exampleKey: 'meeting_share_example',
  },
  document: {
    whatKey: 'document_what',
    connectKey: 'document_connect',
    exampleKey: 'document_example',
  },
  crm_data: {
    whatKey: 'crm_data_what',
    connectKey: 'crm_data_connect',
    exampleKey: 'crm_data_example',
  },
  web_search: {
    whatKey: 'web_search_what',
    connectKey: 'web_search_connect',
    exampleKey: 'web_search_example',
  },
  coding_agent: {
    whatKey: 'coding_agent_what',
    connectKey: 'coding_agent_connect',
    exampleKey: 'coding_agent_example',
  },
  report_xlsx: {
    whatKey: 'report_xlsx_what',
    connectKey: 'report_xlsx_connect',
    exampleKey: 'report_xlsx_example',
  },
  translate: {
    whatKey: 'translate_what',
    connectKey: 'translate_connect',
    exampleKey: 'translate_example',
  },
  doc_edit: {
    whatKey: 'doc_edit_what',
    connectKey: 'doc_edit_connect',
    exampleKey: 'doc_edit_example',
  },
  crm_write: {
    whatKey: 'crm_write_what',
    connectKey: 'crm_write_connect',
    exampleKey: 'crm_write_example',
  },
}
