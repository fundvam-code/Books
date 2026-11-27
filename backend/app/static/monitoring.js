const controls = document.getElementById("monitoring-controls");
const journal = document.getElementById("monitoring-journal");
const checkedAt = document.getElementById("monitoring-checked-at");
const monitoringStatus = document.getElementById("monitoring-status");
const settingsError = document.getElementById("monitoring-settings-error");
const switches = [...(controls?.querySelectorAll('input[type="checkbox"]') ?? [])];
let saving = false;
let settingsVersion = 0;
let savedValues = values();
let journalVersion;

function values() {
    return Object.fromEntries(switches.map((input) => [input.name, input.checked]));
}

function updateToggleColors() {
    const enabled = values().enabled;
    for (const input of switches) {
        input.closest(".monitor-toggle").classList.toggle(
            "active", input.checked && (input.name === "enabled" || enabled),
        );
    }
}

function updateMonitoringStatus(enabled) {
    if (!monitoringStatus) return;
    const text = enabled ? "Включён" : "Выключен";
    if (monitoringStatus.textContent !== text) monitoringStatus.textContent = text;
    monitoringStatus.classList.toggle("online", enabled);
    monitoringStatus.classList.toggle("offline", !enabled);
    document.title = `Мониторинг заказов — ${enabled ? "Вкл" : "Выкл"}`;
}

function applyState(state) {
    for (const input of switches) {
        input.checked = state[input.name];
    }
    savedValues = values();
    if (checkedAt) checkedAt.textContent = state.checked_at;
    updateToggleColors();
    updateMonitoringStatus(state.enabled);
}

async function saveSettings() {
    if (saving) return;
    const current = values();
    updateToggleColors();
    saving = true;
    settingsVersion++;
    settingsError.hidden = true;
    switches.forEach((input) => { input.disabled = true; });
    try {
        const response = await fetch("/monitoring/settings", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(current),
        });
        if (!response.ok) throw new Error("Не удалось сохранить настройки мониторинга");
        applyState(await response.json());
    } catch (error) {
        for (const input of switches) input.checked = savedValues[input.name];
        updateToggleColors();
        settingsError.textContent = "Не удалось сохранить настройки мониторинга. Попробуйте ещё раз.";
        settingsError.hidden = false;
    } finally {
        saving = false;
        switches.forEach((input) => { input.disabled = false; });
    }
}

async function refreshState() {
    if (saving) return;
    const version = settingsVersion;
    try {
        const response = await fetch("/monitoring/state", { cache: "no-store" });
        if (response.ok) {
            const state = await response.json();
            if (!saving && version === settingsVersion) applyState(state);
        }
    } catch {
        // Keep the last displayed state until the server responds.
    }
}

async function refreshJournal() {
    try {
        const response = await fetch("/monitoring/journal", { cache: "no-store" });
        if (!response.ok) return;
        const { items } = await response.json();
        const version = JSON.stringify(items);
        if (journalVersion === version) return;
        journalVersion = version;
        journal.replaceChildren();
        if (!items.length) {
            const row = document.createElement("tr");
            row.className = "monitor-empty";
            const cell = document.createElement("td");
            cell.colSpan = 6;
            cell.textContent = "Записей пока нет";
            row.append(cell);
            journal.append(row);
            return;
        }
        for (const item of items) {
            const row = document.createElement("tr");
            for (const value of [item.time, item.account, item.posting_number, item.label_number || "—"]) {
                const cell = document.createElement("td");
                cell.textContent = value;
                row.append(cell);
            }
            const book = document.createElement("td");
            if (item.image_urls?.length) {
                const summary = document.createElement("span");
                summary.className = "monitor-book";
                for (const url of item.image_urls) {
                    try {
                        if (new URL(url).protocol !== "https:") continue;
                    } catch {
                        continue;
                    }
                    const cover = document.createElement("span");
                    cover.className = "book-cover-wrap";
                    cover.setAttribute("aria-label", "Обложка книги");
                    for (const className of ["book-cover-thumb", "book-cover-zoom"]) {
                        const image = document.createElement("img");
                        image.className = className;
                        image.src = url;
                        image.alt = "";
                        image.loading = "lazy";
                        cover.append(image);
                    }
                    summary.append(cover);
                }
                const title = document.createElement("span");
                title.textContent = item.title;
                summary.append(title);
                book.append(summary);
            } else {
                book.textContent = item.title;
            }
            row.append(book);
            const action = document.createElement("td");
            action.textContent = item.message;
            if (item.error) {
                const detail = document.createElement("div");
                detail.className = "error";
                detail.textContent = item.error;
                action.append(detail);
            }
            if (item.confirm_url) {
                const status = document.createElement("div");
                status.textContent = "Ожидает подтверждения";
                const form = document.createElement("form");
                form.method = "post";
                form.action = item.confirm_url;
                const button = document.createElement("button");
                button.type = "submit";
                button.className = "button secondary";
                button.textContent = "Упаковано";
                form.append(button);
                action.append(status, form);
            }
            row.append(action);
            journal.append(row);
        }
    } catch {
        // Keep the last journal view until the server responds.
    }
}

controls?.addEventListener("change", saveSettings);
refreshState();
window.setInterval(refreshState, 15_000);
if (journal) window.setInterval(refreshJournal, 5_000);
