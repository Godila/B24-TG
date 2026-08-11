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
    loading: true,
    sending: false,
    error: "",
    draft: "",
    dialog: null,
    dialogs: [],
    messages: [],
    templates: [],
    lastId: 0,
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
        await Promise.all([this.loadDialogs(), this.loadTemplates()]);
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
        `/api/dialogs/${this.dialog.id}/messages?limit=100`,
        { credentials: "same-origin" },
      );
      if (!res.ok) throw new Error(`Не удалось загрузить сообщения (${res.status})`);
      this.messages = await res.json();
      this.lastId = this.messages.reduce((m, x) => Math.max(m, x.id), 0);
      this.scrollBottom();
    },

    /** Инкрементальный poll: только сообщения после lastId. */
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
          this.messages.push(...fresh);
          this.lastId = fresh.reduce((m, x) => Math.max(m, x.id), this.lastId);
          this.scrollBottom();
        }
      } catch {
        // Сетевая ошибка poll'а — молча, следующий тик попробует снова.
      }
    },

    startPolling() {
      if (this.pollTimer) clearInterval(this.pollTimer);
      this.pollTimer = setInterval(() => this.poll(), this.POLL_MS);
    },

    async send() {
      const text = this.draft.trim();
      if (!text || !this.dialog || this.sending) return;
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
      if (this.pollTimer) clearTimeout(this.pollTimer);
      // Повтор через 10 сек после ошибки.
      setTimeout(() => this.startPolling(), 10000);
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
