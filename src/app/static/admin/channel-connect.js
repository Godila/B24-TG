/**
 * channel-connect — унифицированный UX подключения канала (TG/MAX).
 *
 * Одна фабрика на оба канала: карточка со статусом, QR (клиентский рендер
 * из qr_link), шаг 2FA-пароля, кнопки старт/отмена. Различия каналов —
 * только в labels (заголовок, подсказка куда сканировать) и пути API.
 *
 * Использование: createChannelConnect({root, channel, labels}).mount();
 * После authorized страница перезвонит onAuthorized (перезагрузка /me).
 */
function createChannelConnect({ root, channel, labels, onAuthorized }) {
  let pollTimer = null;
  let shownLink = null;

  const el = (cls) => root.querySelector("." + cls);

  function statusBadge(status) {
    const map = {
      active: ["подключён", "ok"],
      offline: ["офлайн", "warn"],
      banned: ["заблокирован", "err"],
      waiting: ["ожидание скана…", "wait"],
      password_required: ["нужен 2FA-пароль", "wait"],
      authorized: ["подключено", "ok"],
      expired: ["QR истёк", "err"],
      error: ["ошибка", "err"],
    };
    return map[status] || [status, ""];
  }

  function renderAccount(acc) {
    const [text, cls] = statusBadge(acc ? acc.status : "none");
    el("state").innerHTML = acc
      ? `<span class="badge ${cls}">${text}</span>` +
        `<div class="hint">${acc.name || ""}${acc.phone && !acc.phone.startsWith("TG-mgr") && !acc.phone.startsWith("MAX-") ? " · " + acc.phone : ""}</div>`
      : `<span class="hint">не подключен</span>`;
    const connected = acc && acc.status === "active";
    el("start").style.display = connected ? "none" : "";
    el("start").textContent = acc ? "Переподключить" : labels.startLabel;
  }

  function renderLogin(login) {
    el("qrbox").innerHTML = "";
    el("pwrow").style.display = "none";
    stopPoll();
    if (!login) { el("loginstate").textContent = ""; return; }
    if (login.status === "waiting" && login.qr_link) {
      if (login.qr_link !== shownLink) {
        shownLink = login.qr_link;
        el("qrbox").innerHTML = "";
        new QRCode(el("qrbox"), { text: login.qr_link, width: 220, height: 220 });
      }
      el("loginstate").textContent = labels.scanHint;
      startPoll();
    } else if (login.status === "waiting") {
      el("loginstate").textContent = "Получаем QR-код…";
      startPoll();
    } else if (login.status === "password_required") {
      el("pwrow").style.display = "";
      el("loginstate").textContent = "Введите пароль двухфакторной защиты:";
      startPoll();
    } else if (login.status === "authorized") {
      el("loginstate").textContent = "Готово! Канал активируется автоматически (~20 секунд).";
      if (onAuthorized) setTimeout(onAuthorized, 1500);
    } else if (login.status === "expired") {
      el("loginstate").textContent = "Время ожидания истекло — попробуйте снова.";
    } else if (login.status === "error") {
      el("loginstate").textContent = login.error || "Ошибка входа";
    }
  }

  async function api(path, opts) {
    const res = await fetch(path, Object.assign({ credentials: "same-origin" }, opts));
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw Object.assign(new Error(data.detail || res.status), { status: res.status });
    return data;
  }

  function stopPoll() { if (pollTimer) { clearInterval(pollTimer); pollTimer = null; } }
  function startPoll() {
    stopPoll();
    let failures = 0;
    pollTimer = setInterval(async () => {
      try {
        const login = await api(`/admin/api/onboarding/${channel}/status`);
        failures = 0;
        renderLogin(login);
      } catch (e) {
        // 404 — логина больше нет (стоп); сетевой блип — терпим 3 подряд.
        if (e.status === 404 || ++failures > 3) stopPoll();
      }
    }, 2000);
  }

  async function refresh() {
    try {
      const me = await api("/admin/api/me");
      const acc = (me.accounts || []).find((a) => a.messenger === channel);
      renderAccount(acc);
    } catch (e) {
      el("state").innerHTML = `<span class="hint">нет доступа: ${e.message}</span>`;
    }
  }

  async function start() {
    el("start").disabled = true;
    try {
      const data = await api(`/admin/api/onboarding/${channel}/start`, { method: "POST" });
      if (data.status === "already_active") { await refresh(); return; }
      el("loginstate").textContent = "Получаем QR-код…";
      startPoll();
    } catch (e) {
      el("loginstate").textContent = e.message;
    } finally {
      el("start").disabled = false;
    }
  }

  async function submitPassword() {
    const input = el("pwinput");
    if (!input.value) return;
    try {
      await api(`/admin/api/onboarding/${channel}/password`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ password: input.value }),
      });
      input.value = "";
      el("loginstate").textContent = "Проверяем пароль…";
    } catch (e) {
      el("loginstate").textContent = e.message;
    }
  }

  async function cancel() {
    stopPoll();
    try { await api(`/admin/api/onboarding/${channel}/cancel`, { method: "POST" }); } catch {}
    el("qrbox").innerHTML = "";
    el("loginstate").textContent = "Отменено.";
  }

  function mount() {
    root.innerHTML = `
      <div class="channel-card">
        <h3>${labels.title}</h3>
        <div class="state"></div>
        <button class="btn start" type="button">${labels.startLabel}</button>
        <div class="loginstate hint"></div>
        <div class="qrbox"></div>
        <div class="pwrow" style="display:none">
          <input class="pwinput" type="password" autocomplete="off" placeholder="2FA-пароль">
          <button class="btn btn--sm pwbtn" type="button">Продолжить</button>
        </div>
      </div>`;
    el("start").addEventListener("click", start);
    el("pwbtn").addEventListener("click", submitPassword);
    el("pwinput").addEventListener("keydown", (e) => { if (e.key === "Enter") submitPassword(); });
    refresh();
    // показать недавний логин после перезагрузки страницы
    api(`/admin/api/onboarding/${channel}/status`)
      .then(renderLogin)
      .catch(() => {});
  }

  return { mount, refresh };
}
