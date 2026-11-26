for (const selector of ["hide-printed", "hide-waiting"]) {
    document.getElementById(selector).addEventListener("change", (event) => {
        event.target.form.requestSubmit();
    });
}

const shiftLoading = document.querySelector("[data-shift-loading]");
setTimeout(() => refreshLabelPage(), shiftLoading ? 3000 : 5 * 60 * 1000);

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
