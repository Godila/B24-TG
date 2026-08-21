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
// ponytail: чат-панель дублируется копией в inbox.js (JS-тестов нет); вынести общий core при появлении JS-тестов или 3-й копии. Страж дрейфа: scripts/check_js_sync.py
function chatApp() {
  return {
    POLL_MS: 3000,
    PAGE_SIZE: 100,
    loading: true,
    readonly: false,
    sending: false,
    error: "",
    draft: "",
    pendingFile: null,
    pendingFileUrl: null,
    lightboxSrc: null,
    dialog: null,
    dialogs: [],
    messages: [],
    templates: [],
    lastId: 0,
    hasMore: false,
    pollTimer: null,
    // «Написать первым»: форма + поллинг команды инициализации.
    initOpen: false,
    initBusy: false,
    initError: "",
    accounts: [],
    initMessenger: "",
    initAccountId: null,
    initDest: "",
    initText: "",
    initRemember: false,

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

    /** Тип карточки CRM виджета: data-entity-type (placement) либо 'deal'. */
    get dealKind() {
      return document.body.dataset.entityType || "deal";
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

    /** Право записи в открытый диалог: владелец/участник линии и не read-only
     *  (наблюдатель линии и supervisor-надзор пишут только прочитанное). */
    get canWrite() {
      return !!this.dialog && this.dialog.can_write !== false && !this.readonly;
    },

    async loadDialogs() {
      const url = this.dealId
        ? `/api/dialogs?deal_id=${encodeURIComponent(this.dealId)}` +
          `&entity_type=${encodeURIComponent(this.dealKind)}`
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
     *  «висящих» исходящих (⏳→✓ и ✓→✓✓ при прочтении клиентом): poll
     *  их не возвращает, а оптимистичный пузырь навсегда остался бы с
     *  часиками, хотя сервер уже доставил. */
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
     *  сообщений (pending → sent/error; sent/delivered → read ✓✓).
     *  Новые id не добавляются — их приносит poll. Гард открыт, пока у
     *  исходящих нет терминального статуса (read/error); direction
     *  обязателен: inbound-строки живут в delivered вечно. Окно = размер
     *  отрендеренного списка (cap API 200): sent-сообщение глубже хвоста
     *  иначе никогда не доживёт до ✓✓ без перезагрузки. */
    async refreshPendingStatuses() {
      const unfinished = (m) =>
        m.status === "pending" ||
        (m.direction === "out" && (m.status === "sent" || m.status === "delivered"));
      if (!this.dialog || !this.messages.some(unfinished)) return;
      try {
        const limit = Math.min(200, Math.max(50, this.messages.length));
        const res = await fetch(
          `/api/dialogs/${this.dialog.id}/messages?limit=${limit}`,
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
      if (this.pendingFile) return this.sendFile();
      const text = this.draft.trim();
      if (!text || !this.dialog || this.sending || !this.canWrite) return;
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

    // --- «Написать первым» (только app.js: inbox-копия фичи не имеет) ---

    /** Каналы, из которых есть что выбрать. */
    get initMessengers() {
      return [...new Set(this.accounts.map((a) => a.messenger))];
    },

    /** Аккаунты выбранного канала (для селектора). */
    get initAccounts() {
      return this.accounts.filter((a) => a.messenger === this.initMessenger);
    },

    /** Подсказка ввода зависит от канала: MAX ищет только по телефону. */
    get initDestPlaceholder() {
      return this.initMessenger === "max" ? "Телефон +7…" : "Телефон +7… или @username";
    },

    async openInitiate() {
      this.initOpen = true;
      this.initError = "";
      if (this.accounts.length === 0) {
        try {
          const res = await fetch("/api/accounts", { credentials: "same-origin" });
          if (!res.ok) throw new Error(`Не удалось загрузить аккаунты (${res.status})`);
          this.accounts = await res.json();
        } catch (e) {
          this.showError(e);
          return;
        }
      }
      this.setInitMessenger(this.initMessenger || this.initMessengers[0] || "");
      // Нет доступных аккаунтов — честная ошибка в форме.
      if (!this.initMessenger) this.initError = "Нет доступных аккаунтов — обратитесь к администратору";
    },

    setInitMessenger(m) {
      this.initMessenger = m;
      const list = this.accounts.filter((a) => a.messenger === m);
      const def = list.find((a) => a.is_default);
      this.initAccountId = def ? def.id : list.length === 1 ? list[0].id : null;
    },

    closeInitiate() {
      this.initOpen = false;
      this.initError = "";
    },

    async submitInitiate() {
      if (this.initBusy || !this.dealId || !this.initMessenger) return;
      this.initBusy = true;
      this.initError = "";
      try {
        const res = await fetch("/api/dialogs/initiate", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          credentials: "same-origin",
          body: JSON.stringify({
            messenger: this.initMessenger,
            entity_type: this.dealKind,
            entity_id: Number(this.dealId),
            account_id: this.initAccountId,
            dest: this.initDest.trim(),
            text: this.initText.trim(),
            remember_account: this.initRemember,
          }),
        });
        const body = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(body.detail || `Не удалось начать диалог (${res.status})`);
        // Поллинг команды: linked → открыть диалог, failed → ошибка в форму.
        // Цикл (а не таймер): закрытие формы гасит его флагом initOpen.
        for (let i = 0; i < 80 && this.initOpen; i++) {
          await new Promise((r) => setTimeout(r, 1500));
          const st = await fetch(`/api/dialogs/initiate/${body.id}`, {
            credentials: "same-origin",
          }).then((r) => (r.ok ? r.json() : null));
          if (!st) throw new Error("Команда не найдена");
          if (st.status === "linked") {
            this.initOpen = false;
            this.initDest = "";
            this.initText = "";
            await this.onInitiationLinked(st.dialog_id);
            return;
          }
          if (st.status === "failed") {
            throw new Error(st.error || "Не найден");
          }
        }
        throw new Error("Поиск затянулся — попробуйте ещё раз");
      } catch (e) {
        this.initError = e.message || String(e);
      } finally {
        this.initBusy = false;
      }
    },

    async onInitiationLinked(dialogId) {
      await this.loadDialogs();
      this.dialog = this.dialogs.find((d) => d.id === dialogId) || this.dialogs[0] || null;
      if (this.dialog) {
        await this.loadMessages();
        this.startPolling();
      }
    },

    // --- Медиа-вложения (синхронизированная копия в inbox.js) ---

    /** Скрепка доступна: право записи в диалог, не read-only (TG и MAX умеют файлы). */
    get canAttach() {
      return !!this.dialog && this.canWrite;
    },

    pickFile() {
      if (this.$refs.fileInput) this.$refs.fileInput.click();
    },

    onFilePicked(e) {
      this.setFile(e.target.files && e.target.files[0]);
      e.target.value = ""; // повторный выбор того же файла тоже событие
    },

    onDrop(e) {
      const f = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
      if (f) this.setFile(f);
    },

    onPaste(e) {
      const f = e.clipboardData && e.clipboardData.files && e.clipboardData.files[0];
      if (f) this.setFile(f);
    },

    setFile(f) {
      if (!f) return;
      if (!this.canAttach) {
        this.error = "Вложения недоступны в этом диалоге";
        return;
      }
      if (f.size > 25 * 1024 * 1024) {
        this.error = "Файл больше 25 МБ";
        return;
      }
      this.clearFile();
      this.pendingFile = f;
      this.pendingFileUrl = URL.createObjectURL(f);
      this.error = "";
    },

    clearFile() {
      if (this.pendingFileUrl) URL.revokeObjectURL(this.pendingFileUrl);
      this.pendingFile = null;
      this.pendingFileUrl = null;
    },

    /** Отправка файла: multipart на /media, оптимистичный пузырь с
     *  локальным blob-URL (мгновенное превью до ответа сервера). */
    async sendFile() {
      const file = this.pendingFile;
      if (!file || !this.dialog || this.sending || this.readonly) return;
      this.sending = true;
      this.error = "";
      const caption = this.draft.trim();
      const fileUrl = this.pendingFileUrl;
      const optimistic = {
        id: "optimistic-" + Date.now(),
        direction: "out",
        text: caption || null,
        status: "pending",
        created_at: new Date().toISOString(),
        attachments: [
          {
            id: "local-" + Date.now(),
            type: this.guessAttType(file.type),
            mime_type: file.type || null,
            size: file.size,
            file_name: file.name,
            local_url: fileUrl,
          },
        ],
      };
      this.messages.push(optimistic);
      this.scrollBottom();
      const draftSaved = this.draft;
      this.draft = "";
      // Слот файла освобождён, blob-URL живёт до замены/отката пузыря.
      this.pendingFile = null;
      this.pendingFileUrl = null;
      try {
        const fd = new FormData();
        fd.append("file", file);
        if (caption) fd.append("caption", caption);
        const res = await fetch(`/api/dialogs/${this.dialog.id}/media`, {
          method: "POST",
          credentials: "same-origin",
          body: fd,
        });
        if (!res.ok) {
          const body = await res.json().catch(() => ({}));
          throw new Error(body.detail || `Отправка не удалась (${res.status})`);
        }
        const saved = await res.json();
        const idx = this.messages.findIndex((m) => m.id === optimistic.id);
        if (idx >= 0) this.messages[idx] = saved;
        this.lastId = Math.max(this.lastId, saved.id);
        if (fileUrl) URL.revokeObjectURL(fileUrl);
      } catch (e) {
        const idx = this.messages.findIndex((m) => m.id === optimistic.id);
        if (idx >= 0) this.messages.splice(idx, 1);
        if (fileUrl) URL.revokeObjectURL(fileUrl);
        // Откат: текст и файл возвращаются в композер.
        this.draft = draftSaved;
        this.pendingFile = file;
        this.pendingFileUrl = URL.createObjectURL(file);
        this.showError(e);
      } finally {
        this.sending = false;
      }
    },

    guessAttType(mime) {
      if (!mime) return "file";
      if (mime.startsWith("image/")) return "photo";
      if (mime.startsWith("video/")) return "video";
      if (mime.startsWith("audio/")) return "voice";
      return "file";
    },

    /** Ссылка вложения: локальный blob (оптимистичный пузырь) или API-URL. */
    attSrc(att) {
      return att.local_url || att.file_url || "";
    },

    attKind(att) {
      const mime = att.mime_type || "";
      if (att.type === "photo" || mime.startsWith("image/")) return "photo";
      if (att.type === "video" || mime.startsWith("video/")) return "video";
      if (att.type === "voice" || mime.startsWith("audio/")) return "audio";
      return "file";
    },

    fileLabel(att) {
      const name = att.file_name || "Файл";
      return att.size != null ? `${name} · ${this.formatSize(att.size)}` : name;
    },

    formatSize(bytes) {
      if (bytes == null) return "";
      if (bytes < 1024) return bytes + " Б";
      if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + " КБ";
      if (bytes < 1024 * 1024 * 1024) return (bytes / 1048576).toFixed(1) + " МБ";
      return (bytes / 1073741824).toFixed(2) + " ГБ";
    },

    /** Открыть вложение: фото — лайтбокс внутри страницы, остальное —
     *  скачивание. НИКОГДА не target=_blank: сессионная кука живёт в
     *  партиционированной банке iframe-контекста, верхнеуровневая
     *  навигация её не видит (401 «Не авторизован»). */
    openAttachment(att) {
      if (this.attKind(att) === "photo") {
        this.lightboxSrc = this.attSrc(att);
        return;
      }
      this.downloadAttachment(att);
    },

    closeLightbox() {
      this.lightboxSrc = null;
    },

    /** Скачивание через fetch в контексте страницы (кука прикрепляется,
     *  как у poll) → blob → программный клик по <a download>. */
    async downloadAttachment(att) {
      const url = this.attSrc(att);
      if (!url) return;
      if (url.startsWith("blob:")) {
        window.open(url, "_blank", "noopener");
        return;
      }
      try {
        const res = await fetch(url, { credentials: "same-origin" });
        if (!res.ok) throw new Error(`Не удалось скачать файл (${res.status})`);
        const blob = await res.blob();
        const objUrl = URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = objUrl;
        a.download = att.file_name || "attachment";
        document.body.appendChild(a);
        a.click();
        a.remove();
        setTimeout(() => URL.revokeObjectURL(objUrl), 10000);
      } catch (e) {
        this.showError(e);
      }
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
      // Иконки статусов из спрайта (svg-строки — не юзер-данные, x-html ок);
      // цвета прочитано/ошибка — классы st-read/st-err в CSS.
      const ICON = (name, cls) =>
        '<svg class="i ' + (cls || "") + '" aria-hidden="true">' +
        '<use href="/static/icons.svg#' + name + '"/></svg>';
      if (status === "pending") return ICON("clock");
      if (status === "sent") return ICON("check");
      if (status === "delivered") return ICON("checks");
      if (status === "read") return ICON("checks", "st-read");
      if (status === "error") return ICON("warning", "st-err");
      return "";
    },
  };
}
