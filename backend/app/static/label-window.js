const printImmediately = document.getElementById("print-immediately");
window.addEventListener("storage", (event) => {
    if (event.key === "fbs-print-submission" && event.newValue) {
        document.dispatchEvent(new Event("orders-changed"));
    }
});

if (printImmediately) {
    const preferenceKey = "ozon-label-print-immediately";
    const restorePreference = () => {
        try {
            printImmediately.checked = localStorage.getItem(preferenceKey) === "true";
        } catch {
            // The checkbox still works on the current page when storage is unavailable.
        }
    };

    restorePreference();
    window.addEventListener("pageshow", restorePreference);
    printImmediately.addEventListener("change", () => {
        try {
            localStorage.setItem(preferenceKey, String(printImmediately.checked));
        } catch {
            // Keep the user's current selection even when it cannot be saved.
        }
    });
}

document.addEventListener("click", (event) => {
    const link = event.target.closest("[data-label-open]");
    if (!link || event.defaultPrevented || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
    const form = link.closest("[data-order-label]").querySelector(".label-output");
    event.preventDefault();
    form.requestSubmit(form.querySelector("button[data-status-modal]:not([data-dashboard-print])"));
});
