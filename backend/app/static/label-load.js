const labelJson = async (url, method = "GET") => {
    const response = await fetch(url, { method, headers: { Accept: "application/json" } });
    const data = await response.json();
    if (!response.ok) {
        throw new Error(data.detail || "Не удалось загрузить PDF.");
    }
    return data;
};

for (const form of document.querySelectorAll(".label-load")) {
    form.addEventListener("submit", async (event) => {
        event.preventDefault();
        const button = form.querySelector("button");
        const error = form.querySelector(".error");
        error.hidden = true;
        button.disabled = true;
        button.textContent = "Загружаем PDF…";
        try {
            const { status_url: url } = await labelJson(form.action, "POST");
            let label = await labelJson(url);
            while (label.state === "pending" || label.state === "leased") {
                await new Promise((resolve) => setTimeout(resolve, 2000));
                label = await labelJson(url);
            }
            if (label.state !== "ready") {
                throw new Error(label.error || "PDF недоступен.");
            }
            const link = document.createElement("a");
            link.className = "tag label-link";
            link.href = label.pdf_url;
            link.target = "_blank";
            link.rel = "noopener";
            link.textContent = "PDF загружен";
            link.dataset.labelOpen = "";
            const order = form.closest("[data-order-label]");
            const printButton = order?.querySelector(".label-output button[type=submit]");
            if (printButton) printButton.hidden = true;
            const number = order?.querySelector("[data-label-number]");
            if (number && label.label_number) {
                number.textContent = "№ этикетки " + label.label_number;
                number.hidden = false;
            }
            form.replaceWith(link);
        } catch (failure) {
            button.disabled = false;
            button.textContent = "Повторить загрузку PDF";
            error.textContent = failure.message;
            error.hidden = false;
        }
    });
}
