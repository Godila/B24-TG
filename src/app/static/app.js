/**
 * Bitrix-TG чат-виджет.
 *
 * Alpine.js-компонент: грузит диалоги/сообщения по API, опрашивает новые каждые
 * POLL_MS секунд (polling MVP — WebSocket в Фазе 3b), отправляет сообщения.
 *
 * Контекст сделки приходит через URL-query (?deal_id=) при dev-входе через
 * /dev/login. В проде (placement) deal_id зашит в сессионную куку на сервере,
 * а список диалогов фильтруется по сделке через ?deal_id= если он известен.
 */
function chatApp() {
  return {
    POLL_MS: 3000,
    PAGE_SIZE: 100,
    loading: true,
    readonly: false,
    sending: false,
    error: "",
    draft: "",
    dialog: null,
    dialogs: [],
    messages: [],
    templates: [],
    lastId: 0,
    hasMore: false,
    pollTimer: null,

    /**
     * Текущий deal_id: из query (?deal_id=) при dev-входе через /dev/login,
     * либо из data-deal-id на <body> (внедряется placement-обработчиком при
     * реальном вызове B24, когда URL фиксирован).
     */
    get dealId() {
      const fromQuery = new URLSearchParams(window.location.search).get("deal_id");
      if (fromQuery) return fromQuery;
      return document.body.dataset.dealId || null;
    },

    async init() {
      try {
        await Promise.all([this.loadMe(), this.loadDialogs(), this.loadTemplates()]);
        if (this.dialogs.length > 0) {
          this.dialog = this.dialogs[0];
          await this.loadMessages();
          this.startPolling();
        }
      } catch (e) {
        this.showError(e);
      } finally {
        this.loading = false;
      }
    },

    /** Права текущего менеджера: read-only прячет поле отправки. */
    async loadMe() {
      try {
        const res = await fetch("/api/me", { credentials: "same-origin" });
        if (res.ok) this.readonly = (await res.json()).is_readonly === true;
      } catch {
        // Недоступен /api/me — считаем читателем (fail-closed для отправки).
        this.readonly = true;
      }
    },

    async loadDialogs() {
      const url = this.dealId
        ? `/api/dialogs?deal_id=${encodeURIComponent(this.dealId)}`
        : "/api/dialogs";
      const res = await fetch(url, { credentials: "same-origin" });
      if (!res.ok) throw new Error(`Не удалось загрузить диалоги (${res.status})`);
      this.dialogs = await res.json();
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
      const res = await fetch(
        `/api/dialogs/${this.dialog.id}/messages?limit=${this.PAGE_SIZE}`,
        { credentials: "same-origin" },
      );
      if (!res.ok) throw new Error(`Не удалось загрузить сообщения (${res.status})`);
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

    /** Инкрементальный poll: новые сообщения (since=lastId) + статусы
     *  «висящих» исходящих (⏳→✓): poll их не возвращает, а оптимистичный
     *  пузырь навсегда остался бы с часиками, хотя сервер уже доставил. */
    async poll() {
      if (!this.dialog || this.sending) return;
      try {
        const res = await fetch(
          `/api/dialogs/${this.dialog.id}/messages?since=${this.lastId}`,
          { credentials: "same-origin" },
        );
        if (!res.ok) return;
        const fresh = await res.json();
        if (fresh.length > 0) {
          // Дедупликация по id: защищает от гонки с оптимистичной отправкой,
          // когда poll успел забрать реальное сообщение раньше, чем пришёл
          // ответ POST (тогда в массиве оказались бы две записи с одним id).
          const known = new Set(this.messages.map((m) => m.id));
          const unseen = fresh.filter((m) => !known.has(m.id));
          if (unseen.length > 0) {
            this.messages.push(...unseen);
            this.lastId = unseen.reduce(
              (m, x) => Math.max(m, x.id),
              this.lastId,
            );
            this.scrollBottom();
          }
        }
        await this.refreshPendingStatuses();
      } catch {
        // Сетевая ошибка poll'а — молча, следующий тик попробует снова.
      }
    },

    /** Перечитать хвост истории и обновить статусы уже показанных
     *  сообщений (pending → sent/error). Новые id не добавляются —
     *  их приносит poll. */
    async refreshPendingStatuses() {
      if (!this.messages.some((m) => m.status === "pending")) return;
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
      if (!text || !this.dialog || this.sending || this.readonly) return;
      this.sending = true;
      this.error = "";
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

    /** Короткая метка канала для бейджа в шапке диалога. */
    channelLabel(messenger) {
      if (messenger === "max") return "MAX";
      if (messenger === "tg") return "TG";
      return "";
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
