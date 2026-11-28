for (const selector of ["hide-printed", "hide-waiting"]) {
    document.getElementById(selector).addEventListener("change", (event) => {
        event.target.form.requestSubmit();
    });
}

const shiftLoading = document.querySelector("[data-shift-loading]");
let refreshingOrders = false;
let refreshAgain = false;

async function refreshOrders() {
    if (refreshingOrders) {
        refreshAgain = true;
        return;
    }
    refreshingOrders = true;
    try {
        const response = await fetch(window.location.href, {
            cache: "no-store",
            signal: AbortSignal.timeout(10000),
        });
        if (!response.ok) return;
        const page = new DOMParser().parseFromString(await response.text(), "text/html");
        const incoming = page.querySelector("[data-orders-content]");
        const current = document.querySelector("[data-orders-content]");
        if (!incoming || current.innerHTML === incoming.innerHTML) return;
        if (current.querySelector(".label-load button:disabled")) return;

        const options = new Map();
        function key(input) {
            return input.form.closest("[data-order-key]")?.dataset.orderKey
                || input.form.elements.account?.value;
        }
        for (const input of current.querySelectorAll('input[type="checkbox"][name="with_title"]')) {
            options.set(key(input), input.checked);
        }
        for (const input of incoming.querySelectorAll('input[type="checkbox"][name="with_title"]')) {
            if (options.has(key(input))) input.checked = options.get(key(input));
        }
        current.replaceWith(incoming);
    } catch {
        // Keep the displayed orders and retry on the next refresh.
    } finally {
        refreshingOrders = false;
        if (refreshAgain) {
            refreshAgain = false;
            refreshOrders();
        }
    }
}

document.addEventListener("orders-changed", () => {
    refreshOrders();
    refreshDashboardCounts();
});
setInterval(() => {
    if (!document.hidden) refreshOrders();
}, 15000);
if (shiftLoading) setTimeout(refreshOrders, 3000);

const stageCounts = document.querySelectorAll("[data-stage-count]");
const activeCabinetTab = document.querySelector('.tabs [aria-current="page"]');
let loadingStageCounts = false;

async function refreshStageCounts() {
    if (loadingStageCounts) return;
    loadingStageCounts = true;
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 4000);
    try {
        const response = await fetch("/orders/stage-counts", { cache: "no-store", signal: controller.signal });
        if (!response.ok) return;
        const counts = await response.json();
        for (const node of stageCounts) {
            const count = counts[node.dataset.stageCount];
            if (Array.isArray(count) && count.length === 2 && count.every(Number.isInteger)) {
                node.textContent = count.join(" / ");
            }
        }
        if (activeCabinetTab) document.title = activeCabinetTab.textContent.trim() + " — Заказы FBS";
    } catch {
        // Keep the last counts until the dashboard responds again.
    } finally {
        clearTimeout(timeout);
        loadingStageCounts = false;
    }
}

const printedTotalValue = document.querySelector("[data-printed-total-value]");

async function refreshPrintedTotal() {
    if (!printedTotalValue) return;
    try {
        const response = await fetch("/orders/printed-total", { cache: "no-store" });
        if (!response.ok) return;
        const data = await response.json();
        if (Number.isInteger(data.total)) printedTotalValue.textContent = data.total;
    } catch {
        // Keep the last total until the dashboard responds again.
    }
}

function refreshDashboardCounts() {
    refreshStageCounts();
    refreshPrintedTotal();
}

setInterval(refreshDashboardCounts, 60_000);
document.addEventListener("visibilitychange", () => {
    if (!document.hidden) refreshDashboardCounts();
});
