/**
 * ЧатМост — «Чаты» (общий мессенджер, пункт левого меню Битрикс24).
 *
 * Чат-панель (loadMessages/loadOlder/poll-сообщений/refreshPendingStatuses/
 * send/scrollBottom/statusLabel/formatTime/channelLabel/showError) —
 * СИНХРОНИЗИРОВАННАЯ КОПИЯ из app.js: рефакторинг прод-виджета сделки без
 * JS-тестов — недопустимый риск; при правке логики там — перенести сюда.
 * Отличия inbox: список с агрегатами (неотвеченные/непрочитанные), фильтр
 * канала, гашение непрочитанных при открытии, supervisor-просмотр чужих
 * диалогов без composer.
 */
function inboxApp() {
  return {
    POLL_MS: 3000,
    PAGE_SIZE: 100,
    loading: true,
    readonly: false,
    isSupervisor: false,
    sending: false,
    error: "",
    draft: "",
    // Активный диалог — объект из this.dialogs (InboxDialogOut); после
    // каждого refreshDialogs ссылка перепривязывается по activeId.
    dialog: null,
    activeId: null,
    dialogs: [],
    messages: [],
    templates: [],
    lastId: 0,
    hasMore: false,
    pollTimer: null,
    filter: "all", // all | tg | max
    // Supervisor: фильтр по ответственному (0 = все, -1 = без ответственного).
    managerFilter: 0,
    // Триггер вкладки (UX-10): счётчик неотвеченных в document.title + звук.
    baseTitle: "ЧатМост — Чаты",
    prevUnanswered: null,
    // A11y: анонс новых входящих для скринридеров (aria-live).
    liveAnnouncement: "",
    // «↓ Новые сообщения» — когда пришли сообщения, а пользователь выше.
    newBelow: false,

    // --- Секции списка (неотвеченные собираются наверх) ---
    get managerOptions() {
      const map = new Map();
      for (const d of this.dialogs) {
        if (d.assigned_manager_id == null) continue;
        if (!map.has(d.assigned_manager_id)) {
          map.set(d.assigned_manager_id, d.assigned_manager_name || "—");
        }
      }
      return [...map.entries()]
        .map(([id, name]) => ({ id, name }))
        .sort((a, b) => a.name.localeCompare(b.name, "ru"));
    },
    get filteredDialogs() {
      return this.dialogs.filter((d) => {
        const byChannel = this.filter === "all" || d.messenger === this.filter;
        const mf = this.managerFilter;
        const byManager =
          !mf ||
          (mf === -1
            ? d.assigned_manager_id == null
            : d.assigned_manager_id === mf);
        return byChannel && byManager;
      });
    },
    get unansweredList() {
      return this.filteredDialogs.filter((d) => d.unanswered_count > 0);
    },
    get answeredList() {
      return this.filteredDialogs.filter((d) => d.unanswered_count === 0);
    },
    get canWrite() {
      return !!this.dialog && this.dialog.is_mine && !this.readonly;
    },

    async init() {
      document.title = this.baseTitle;
      // Возврат на вкладку снимает «(N)» из заголовка.
      document.addEventListener("visibilitychange", () => {
        if (!document.hidden) {
          document.title = this.baseTitle;
          this.newBelow = false;
        }
      });
      try {
        await Promise.all([this.loadMe(), this.refreshDialogs(), this.loadTemplates()]);
        const first = this.unansweredList[0] || this.dialogs[0];
        if (first) await this.openDialog(first);
        this.startPolling();
      } catch (e) {
        this.showError(e);
      } finally {
        this.loading = false;
      }
    },

    /** Права текущего менеджера: role → supervisor-режим, is_readonly. */
    async loadMe() {
      try {
        const res = await fetch("/api/me", { credentials: "same-origin" });
        if (res.ok) {
          const me = await res.json();
          this.readonly = me.is_readonly === true;
          this.isSupervisor = me.role === "supervisor";
        }
      } catch {
        // Недоступен /api/me — считаем читателем (fail-closed для отправки).
        this.readonly = true;
      }
    },

    async refreshDialogs() {
      const res = await fetch("/api/inbox/dialogs", { credentials: "same-origin" });
      if (!res.ok) throw new Error(`Не удалось загрузить диалоги (${res.status})`);
      this.dialogs = await res.json();
      // Массив пересобран — перепривязываем активный диалог (свежие счётчики).
      if (this.activeId !== null) {
        this.dialog = this.dialogs.find((d) => d.id === this.activeId) || null;
        if (this.dialog === null) {
          // Активный диалог исчез (архив/переназначение) — чистим панель.
          this.activeId = null;
          this.messages = [];
        }
      }
      // Триггер вкладки: неотвеченных стало больше — счётчик в title + звук.
      const total = this.dialogs.reduce((s, d) => s + (d.unanswered_count || 0), 0);
      if (this.prevUnanswered !== null && total > this.prevUnanswered) {
        this.notifyUnanswered(total);
      }
      this.prevUnanswered = total;
    },

    /** Заголовок вкладки «(N) ЧатМост — Чаты» + короткий звук (UX-10). */
    notifyUnanswered(total) {
      document.title = `(${total}) ${this.baseTitle}`;
      this.playChime();
    },

    /** Двухтональный «динь» через WebAudio; звук опционален — молча при запрете. */
    playChime() {
      try {
        const AC = window.AudioContext || window.webkitAudioContext;
        if (!AC) return;
        this._audioCtx = this._audioCtx || new AC();
        if (this._audioCtx.state === "suspended") {
          this._audioCtx.resume().catch(() => {});
        }
        const t = this._audioCtx.currentTime;
        [
          [880, 0],
          [660, 0.12],
        ].forEach(([freq, at]) => {
          const osc = this._audioCtx.createOscillator();
          const gain = this._audioCtx.createGain();
          osc.type = "sine";
          osc.frequency.value = freq;
          gain.gain.setValueAtTime(0.001, t + at);
          gain.gain.exponentialRampToValueAtTime(0.06, t + at + 0.02);
          gain.gain.exponentialRampToValueAtTime(0.001, t + at + 0.18);
          osc.connect(gain).connect(this._audioCtx.destination);
          osc.start(t + at);
          osc.stop(t + at + 0.2);
        });
      } catch {
        // Звук — опциональное усиление, не критическая функция.
      }
    },

    async openDialog(d) {
      if (this.dialog && this.dialog.id === d.id) return;
      this.activeId = d.id;
      this.dialog = d;
      this.messages = [];
      this.lastId = 0;
      this.hasMore = false;
      await this.loadMessages();
      this.markRead(d);
    },

    /** Гасит непрочитанные владельца; supervisor чужой диалог не гасит. */
    async markRead(d) {
      if (!d || !d.is_mine) return;
      d.unread_count = 0; // оптимистично; при сбое poll вернёт счётчик
      try {
        await fetch(`/api/inbox/dialogs/${d.id}/read`, {
          method: "POST",
          credentials: "same-origin",
        });
      } catch {
        // Не критично: следующий poll скорректирует счётчик из списка.
      }
    },

    async loadTemplates() {
      try {
        const res = await fetch("/api/templates", { credentials: "same-origin" });
        if (res.ok) this.templates = await res.json();
      } catch {
        // Шаблоны опциональны — не блокируем чат.
      }
    },

    async loadMessages() {
      if (!this.dialog) return;
      const dlgId = this.dialog.id;
      const res = await fetch(
        `/api/dialogs/${dlgId}/messages?limit=${this.PAGE_SIZE}`,
        { credentials: "same-origin" },
      );
      if (!res.ok) throw new Error(`Не удалось загрузить сообщения (${res.status})`);
      // Устарел: пользователь переключил диалог, пока шёл ответ.
      if (!this.dialog || this.dialog.id !== dlgId) return;
      // API отдаёт новейшие N (DESC) — разворачиваем в ASC для рендера.
      const data = await res.json();
      this.messages = data.reverse();
      this.lastId = this.messages.reduce((m, x) => Math.max(m, x.id), 0);
      this.hasMore = data.length === this.PAGE_SIZE;
      this.scrollBottom();
    },

    /** Догрузка истории: страница старее самого раннего показанного сообщения. */
    async loadOlder() {
      if (!this.dialog || !this.hasMore || this.messages.length === 0) return;
      const oldestId = this.messages[0].id;
      if (typeof oldestId !== "number") return; // оптимистичный пузырь без id
      const res = await fetch(
        `/api/dialogs/${this.dialog.id}/messages?limit=${this.PAGE_SIZE}&before=${oldestId}`,
        { credentials: "same-origin" },
      );
      if (!res.ok) return; // не критично — кнопку можно нажать снова
      const data = await res.json();
      this.hasMore = data.length === this.PAGE_SIZE;
      if (data.length > 0) {
        // Сохраняем позицию скролла: prepend меняет scrollHeight.
        const el = this.$refs.messages;
        const offset = el ? el.scrollHeight - el.scrollTop : 0;
        this.messages = [...data.reverse(), ...this.messages];
        this.$nextTick(() => {
          if (el) el.scrollTop = el.scrollHeight - offset;
        });
      }
    },

    /** Poll-тик: список (счётчики/сортировка) + новые сообщения активного
     *  диалога + статусы «висящих» исходящих. Список обновляем ВСЕГДА —
     *  иначе у менеджера без активного диалога первый входящий не
     *  появился бы до ручной перезагрузки страницы. */
    async poll() {
      if (this.sending) return;
      try {
        await this.refreshDialogs();
        if (!this.dialog) return;
        const dlgId = this.dialog.id;
        const res = await fetch(
          `/api/dialogs/${dlgId}/messages?since=${this.lastId}`,
          { credentials: "same-origin" },
        );
        if (!res.ok) return;
        // Устарел: диалог сменился, пока шёл ответ — не смешиваем ленты.
        if (!this.dialog || this.dialog.id !== dlgId) return;
        const fresh = await res.json();
        if (fresh.length > 0) {
          // Дедупликация по id: защищает от гонки с оптимистичной отправкой.
          const known = new Set(this.messages.map((m) => m.id));
          const unseen = fresh.filter((m) => !known.has(m.id));
          if (unseen.length > 0) {
            // Scroll-anchor (UX-05): прыгаем вниз, только если пользователь
            // и так у низа; иначе — тихая пилюля «↓ Новые сообщения».
            const el = this.$refs.messages;
            const nearBottom = el
              ? el.scrollHeight - el.scrollTop - el.clientHeight < 120
              : true;
            const hadInbound = unseen.some((m) => m.direction === "in");
            this.messages.push(...unseen);
            this.lastId = unseen.reduce(
              (m, x) => Math.max(m, x.id),
              this.lastId,
            );
            if (nearBottom) {
              this.scrollBottom();
            } else {
              this.newBelow = true;
            }
            if (hadInbound) {
              // A11y: анонс для скринридеров (aria-live).
              this.liveAnnouncement =
                "Новое сообщение от " + (this.dialog.contact_name || "клиента");
              setTimeout(() => (this.liveAnnouncement = ""), 4000);
              // Пользователь смотрит на диалог — входящие сразу прочитаны.
              this.markRead(this.dialog);
            }
          }
        }
        await this.refreshPendingStatuses();
      } catch {
        // Сетевая ошибка poll'а — молча, следующий тик попробует снова.
      }
    },

    jumpNew() {
      this.newBelow = false;
      this.scrollBottom();
    },

    /** Перечитать хвост истории и обновить статусы уже показанных
     *  сообщений (pending → sent/error). Новые id не добавляются —
     *  их приносит poll. */
    async refreshPendingStatuses() {
      if (!this.dialog || !this.messages.some((m) => m.status === "pending")) return;
      try {
        const res = await fetch(
          `/api/dialogs/${this.dialog.id}/messages?limit=50`,
          { credentials: "same-origin" },
        );
        if (!res.ok) return;
        const tail = await res.json(); // DESC — новейшие 50
        const byId = new Map(tail.map((m) => [m.id, m]));
        this.messages = this.messages.map((m) => byId.get(m.id) ?? m);
      } catch {
        // Не критично: следующий poll попробует снова.
      }
    },

    startPolling() {
      this.stopPolling();
      this.pollTimer = setInterval(() => this.poll(), this.POLL_MS);
    },

    stopPolling() {
      if (this.pollTimer) {
        clearInterval(this.pollTimer);
        this.pollTimer = null;
      }
    },

    async send() {
      const text = this.draft.trim();
      if (!text || !this.dialog || this.sending || !this.canWrite) return;
      this.sending = true;
      this.error = "";
      const targetId = this.dialog.id;
      // Оптимистичный рендер: показываем сообщение сразу как pending.
      const optimistic = {
        id: "optimistic-" + Date.now(),
        direction: "out",
        text,
        status: "pending",
        created_at: new Date().toISOString(),
      };
      this.messages.push(optimistic);
      this.scrollBottom();
      const draftSaved = this.draft;
      this.draft = "";
      try {
        const res = await fetch(`/api/dialogs/${this.dialog.id}/messages`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          credentials: "same-origin",
          body: JSON.stringify({ text }),
        });
        if (!res.ok) {
          const body = await res.json().catch(() => ({}));
          throw new Error(body.detail || `Отправка не удалась (${res.status})`);
        }
        const saved = await res.json();
        // Устарел: диалог сменился, пока шла отправка — сообщение попадёт
        // в историю при следующем открытии диалога, здесь не мешаем.
        if (!this.dialog || this.dialog.id !== targetId) return;
        // Заменяем оптимистичную запись на реальную (с id и статусом).
        const idx = this.messages.findIndex((m) => m.id === optimistic.id);
        if (idx >= 0) this.messages[idx] = saved;
        this.lastId = Math.max(this.lastId, saved.id);
      } catch (e) {
        // Откат: возвращаем текст в поле, убираем оптимистичный пузырь.
        const idx = this.messages.findIndex((m) => m.id === optimistic.id);
        if (idx >= 0) this.messages.splice(idx, 1);
        this.draft = draftSaved;
        this.showError(e);
      } finally {
        this.sending = false;
      }
    },

    useTemplate(body) {
      this.draft = this.draft ? this.draft + "\n" + body : body;
    },

    scrollBottom() {
      this.$nextTick(() => {
        const el = this.$refs.messages;
        if (el) el.scrollTop = el.scrollHeight;
      });
    },

    showError(e) {
      this.error = e && e.message ? e.message : String(e);
      this.stopPolling();
      // Повтор через 10 сек: если инициализация упала (например, протухла
      // кука) — пробуем заново; иначе просто возобновляем poll.
      setTimeout(() => {
        this.error = "";
        if (this.dialog) {
          this.startPolling();
        } else {
          this.init();
        }
      }, 10000);
    },

    /** Превью последнего сообщения в списке диалогов. «Вы:» — только в
     *  своих диалогах (supervisor в чужих видит ответ ответственного). */
    preview(d) {
      if (!d.last_message_text) return "";
      return d.is_mine && d.last_message_direction === "out"
        ? "Вы: " + d.last_message_text
        : d.last_message_text;
    },

    /** Короткая метка канала для бейджа. */
    channelLabel(messenger) {
      if (messenger === "max") return "MAX";
      if (messenger === "tg") return "TG";
      return "";
    },

    /** Время в списке (UX-05): относительное — «сейчас»/«N мин», сегодня —
     *  HH:MM, старее — DD.MM (DD.MM.YY в прошлом году). */
    listTime(iso) {
      if (!iso) return "";
      const d = new Date(iso);
      if (isNaN(d)) return "";
      const diffMs = Date.now() - d.getTime();
      if (diffMs < 60 * 1000) return "сейчас";
      if (diffMs < 60 * 60 * 1000) return Math.floor(diffMs / 60000) + " мин";
      const now = new Date();
      if (d.toDateString() === now.toDateString()) {
        return d.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
      }
      const opts =
        d.getFullYear() === now.getFullYear()
          ? { day: "2-digit", month: "2-digit" }
          : { day: "2-digit", month: "2-digit", year: "2-digit" };
      return d.toLocaleDateString("ru-RU", opts);
    },

    /** Инициалы контакта для аватара строки (паттерн .m-avatar, UX-06). */
    initials(name) {
      return (name || "?")
        .trim()
        .split(/\s+/)
        .slice(0, 2)
        .map((w) => w[0])
        .join("")
        .toUpperCase();
    },

    /** Возраст ожидания ответа (UX-07, signature): «ждёт N мин/ч». */
    waitAge(d) {
      if (!d.last_msg_at || d.unanswered_count === 0) return "";
      const mins = Math.floor((Date.now() - new Date(d.last_msg_at).getTime()) / 60000);
      if (mins < 1) return "ждёт <1 мин";
      if (mins < 60) return "ждёт " + mins + " мин";
      if (mins < 24 * 60) {
        return "ждёт " + Math.floor(mins / 60) + " ч " + (mins % 60) + " мин";
      }
      return "ждёт " + Math.floor(mins / 1440) + " дн";
    },

    /** Ожидание дольше часа — тревожный тон (жёлтый → красный, UX-07). */
    waitAgeHot(d) {
      if (!d.last_msg_at || d.unanswered_count === 0) return false;
      return Date.now() - new Date(d.last_msg_at).getTime() >= 60 * 60000;
    },

    formatTime(iso) {
      if (!iso) return "";
      try {
        const d = new Date(iso);
        return d.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
      } catch {
        return "";
      }
    },

    statusLabel(direction, status) {
      if (direction !== "out") return "";
      const map = {
        pending: "⏳",
        sent: "✓",
        delivered: "✓✓",
        read: "✓✓",
        error: "⚠",
      };
      return map[status] || "";
    },
  };
}
