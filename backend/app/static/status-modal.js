(() => {
    const notices = document.querySelector(".order-notices");
    const template = document.getElementById("order-notice");
    const active = new Set();
    let previewCount = 0;

    function errorText(payload) {
        const detail = payload?.detail;
        if (Array.isArray(detail)) return detail.map((item) => item.msg || String(item)).join("; ");
        return detail ? String(detail) : "";
    }

    async function readPayload(response) {
        const text = await response.text();
        try {
            return text ? JSON.parse(text) : {};
        } catch {
            return { detail: text.trim().slice(0, 300) };
        }
    }

    function notice(title) {
        const card = template.content.firstElementChild.cloneNode(true);
        card.querySelector("[data-status-title]").textContent = title;
        card.querySelector("[data-status-close]").addEventListener("click", () => card.remove());
        notices.prepend(card);
        return card;
    }

    function render(card, data) {
        const message = card.querySelector("[data-status-message]");
        message.textContent = data.message;
        message.classList.toggle("error", data.ok === false);
        card.querySelector("[data-status-close]").hidden = !data.done;
    }

    function dismissLater(card, data) {
        const duration = data.ok === false || data.message?.includes("без названия") ? 20000 : 6000;
        setTimeout(() => card.remove(), duration);
    }

    async function pollStatus(url, card) {
        const deadline = Date.now() + 5 * 60 * 1000;
        let failures = 0;
        let assemblyRefreshed = false;
        while (Date.now() < deadline) {
            try {
                const response = await fetch(url, {
                    headers: { Accept: "application/json" },
                    cache: "no-store",
                    signal: AbortSignal.timeout(10000),
                });
                const data = await readPayload(response);
                if (!response.ok) throw new Error(errorText(data) || `Ошибка ${response.status}`);
                if (typeof data.done !== "boolean") throw new Error("Сервер не вернул статус обработки.");
                failures = 0;
                render(card, data);
                if (data.assembled && !assemblyRefreshed) {
                    assemblyRefreshed = true;
                    document.dispatchEvent(new Event("orders-changed"));
                }
                if (data.done) {
                    dismissLater(card, data);
                    return;
                }
            } catch (error) {
                if (++failures >= 3) throw error;
            }
            await new Promise((resolve) => setTimeout(resolve, 2000));
        }
        throw new Error("Обработка продолжается. Текущий результат доступен по ссылке.");
    }

    document.addEventListener("submit", async (event) => {
        const form = event.target;
        const button = event.submitter;
        if (!(form instanceof HTMLFormElement) || !button?.hasAttribute("data-status-modal")) return;
        const action = button.hasAttribute("formaction") ? button.formAction : form.action;
        const order = form.closest("[data-order-key]")?.dataset.orderKey;
        const title = [button.textContent.trim(), order?.split("/").pop()].filter(Boolean).join(" · ");
        const autoprint = form.elements.autoprint;
        const directPrint = button.hasAttribute("data-dashboard-print");
        if (autoprint && !directPrint && !document.getElementById("print-immediately")?.checked) {
            const card = notice("Предпросмотр · " + title);
            render(card, { done: true, message: "PDF откроется после подготовки." });
            const frame = document.createElement("iframe");
            frame.name = "label-preview-" + ++previewCount;
            frame.title = "Предпросмотр PDF этикеток";
            card.append(frame);
            autoprint.disabled = true;
            form.target = frame.name;
            setTimeout(() => form.removeAttribute("target"), 0);
            return;
        }
        event.preventDefault();
        const key = order || action + ":" + (form.elements.account?.value || "");
        if (active.has(key)) return;
        active.add(key);
        const card = notice(title);
        render(card, { message: "Отправляем запрос…" });
        const body = new FormData(form);
        if (autoprint && !directPrint) body.set("autoprint", "true");
        if (button.name) body.append(button.name, button.value);
        button.disabled = true;
        try {
            const response = await fetch(action, {
                method: "POST",
                body,
                headers: { Accept: "application/json" },
                signal: AbortSignal.timeout(15000),
            });
            const payload = await readPayload(response);
            if (!response.ok) throw new Error(errorText(payload) || `Ошибка ${response.status}`);
            if (!payload.status_url) throw new Error("Сервер не вернул статус обработки.");
            const link = card.querySelector("[data-status-link]");
            link.href = payload.status_url;
            link.hidden = false;
            await pollStatus(payload.status_url, card);
        } catch (error) {
            const result = { done: true, ok: false, message: error.message || "Не удалось получить статус." };
            render(card, result);
            dismissLater(card, result);
        } finally {
            active.delete(key);
            button.disabled = false;
            document.dispatchEvent(new Event("orders-changed"));
        }
    });
})();
