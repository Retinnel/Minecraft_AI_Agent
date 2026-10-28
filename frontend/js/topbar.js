// Общая шапка демонстрационных страниц: навигация по сайту и переключатель режима.
//
// Режим живёт в сессии ассистента на сервере (orchestrator.session) и управляет составом
// действий, которые видит модель. Переключатель ходит в те же GET/POST /api/rp/mode, что и
// оверлей мода, поэтому режим, выставленный здесь, действует и в игре.
(function () {
  const PAGES = [
    { href: "/chat", title: "Игровой чат" },
    { href: "/compare", title: "Сравнение конфигураций" },
    { href: "/", title: "Панель" },
    { href: "/admin", title: "Логи" },
    { href: "/admin/challenges", title: "Задания" },
    { href: "/admin/settings", title: "Настройки" },
  ];
  const MODES = [
    { key: "creative", title: "Творческий" },
    { key: "survival", title: "Выживание" },
  ];

  const host = document.getElementById("topbar");
  if (!host) return;

  const style = document.createElement("style");
  style.textContent = `
    #topbar { display: flex; align-items: center; gap: 18px; flex-wrap: wrap; width: 100%; }
    #topbar .pages { display: flex; gap: 4px; flex-wrap: wrap; }
    #topbar .pages a { color: var(--muted, #8b9bb0); text-decoration: none; padding: 6px 12px;
      border-radius: 8px; font-size: 15px; font-weight: 600; }
    #topbar .pages a:hover { color: var(--text, #e6edf6); background: rgba(78, 168, 255, 0.12); }
    #topbar .pages a.current { color: var(--accent, #4ea8ff); background: rgba(78, 168, 255, 0.12); }
    #topbar .mode { margin-left: auto; display: flex; align-items: center; gap: 8px; }
    #topbar .mode .label { color: var(--muted, #8b9bb0); font-size: 14px; }
    #topbar .mode .switch { display: inline-flex; border: 1px solid var(--line, #2a3546);
      border-radius: 999px; overflow: hidden; }
    #topbar .mode button { background: transparent; color: var(--muted, #8b9bb0); border: 0;
      padding: 6px 14px; font-size: 14px; font-weight: 600; cursor: pointer; }
    #topbar .mode button.on { background: var(--accent, #4ea8ff); color: #0b1220; cursor: default; }
    #topbar .mode button:disabled { opacity: .6; }
    #topbar .model { color: var(--muted, #8b9bb0); font-size: 14px; }
  `;
  document.head.appendChild(style);

  const pages = document.createElement("div");
  pages.className = "pages";
  for (const page of PAGES) {
    const a = document.createElement("a");
    a.href = page.href;
    a.textContent = page.title;
    if (location.pathname === page.href) a.className = "current";
    pages.appendChild(a);
  }

  const mode = document.createElement("div");
  mode.className = "mode";
  mode.innerHTML = '<span class="label">Режим</span><span class="switch"></span>';
  const buttons = {};
  for (const item of MODES) {
    const button = document.createElement("button");
    button.textContent = item.title;
    button.onclick = () => setMode(item.key);
    buttons[item.key] = button;
    mode.querySelector(".switch").appendChild(button);
  }

  const model = document.createElement("span");
  model.className = "model";

  host.appendChild(pages);
  host.appendChild(model);
  host.appendChild(mode);

  function show(current) {
    for (const key in buttons) buttons[key].className = key === current ? "on" : "";
    document.dispatchEvent(new CustomEvent("assistant-mode", { detail: { mode: current } }));
  }

  async function setMode(key) {
    for (const k in buttons) buttons[k].disabled = true;
    try {
      const resp = await fetch("/api/rp/mode", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ mode: key }),
      });
      const data = await resp.json();
      if (data.ok) show(data.mode);
    } catch (e) { /* сервер недоступен — переключатель остаётся как был */ }
    for (const k in buttons) buttons[k].disabled = false;
  }

  fetch("/api/rp/mode").then(r => r.json()).then(d => show(d.mode)).catch(() => {});
  fetch("/api/compare/model").then(r => r.json()).then(m => {
    model.textContent = "модель: " + (m.name || "?") + (m.provider ? " (" + m.provider + ")" : "");
  }).catch(() => {});
})();
