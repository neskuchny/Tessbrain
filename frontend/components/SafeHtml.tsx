"use client";

/*
 * SafeHtml — вывести HTML, пришедший из данных, не отдав чужой код браузеру.
 *
 * Санитайзер работает через DOMParser, а его на сервере нет. Если просто
 * позвать его в разметке, серверный проход отдаст пусто, клиентский —
 * содержимое, и React пожалуется на расхождение при гидрации. Поэтому здесь
 * явный порядок: до монтирования показываем запасной текст (обычно
 * markdown-версию), после — вычищенный HTML. Оба прохода детерминированы.
 */

import { useEffect, useState } from "react";
import { sanitizeHtml } from "@/lib/sanitizeHtml";

export default function SafeHtml({
  html,
  fallback = null,
  className,
}: {
  html?: string | null;
  fallback?: React.ReactNode;
  className?: string;
}) {
  const [clean, setClean] = useState<string | null>(null);

  useEffect(() => {
    setClean(sanitizeHtml(html));
  }, [html]);

  if (clean === null) return <>{fallback}</>;
  if (!clean) return <>{fallback}</>;
  return <div className={className} dangerouslySetInnerHTML={{ __html: clean }} />;
}
