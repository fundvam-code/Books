document.getElementById("hide-printed").addEventListener("change", (event) => {
    event.target.form.requestSubmit();
});

const shiftLoading = document.querySelector("[data-shift-loading]");
setTimeout(() => refreshLabelPage(), shiftLoading ? 3000 : 5 * 60 * 1000);

const printCounts = document.querySelectorAll("[data-print-count]");
const activeCabinetTab = document.querySelector('.tabs [aria-current="page"]');
let loadingPrintCounts = false;

async function refreshPrintCounts() {
    if (loadingPrintCounts) return;
    loadingPrintCounts = true;
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 4000);
    try {
        const response = await fetch("/orders/print-counts", { cache: "no-store", signal: controller.signal });
        if (!response.ok) return;
        const counts = await response.json();
        for (const node of printCounts) {
            const count = counts[node.dataset.printCount];
            if (Number.isInteger(count)) node.textContent = count;
        }
        if (activeCabinetTab) document.title = activeCabinetTab.textContent.trim() + " — Заказы FBS";
    } catch {
        // Keep the last counts until the dashboard responds again.
    } finally {
        clearTimeout(timeout);
        loadingPrintCounts = false;
    }
}

setInterval(refreshPrintCounts, 5000);
document.addEventListener("visibilitychange", () => {
    if (!document.hidden) refreshPrintCounts();
});
