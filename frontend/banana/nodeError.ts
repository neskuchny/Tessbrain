// Ошибка узла на языке интерфейса.
//
// Стор доски (workflowStore) хуков не имеет, поэтому кладёт в node.data.error
// не готовую фразу, а КЛЮЧ с маркером «i18n:». Перевод делается здесь, в
// месте отрисовки. Сообщения от сервера и сети приходят обычным текстом и
// показываются как есть — переводить их нечем и незачем.

export function nodeErrorText(
  error: string | null | undefined,
  t: (key: string) => string,
): string {
  const raw = (error || "").trim();
  if (!raw) return t("error_fallback");
  return raw.startsWith("i18n:") ? t(raw.slice(5)) : raw;
}
