document.addEventListener("DOMContentLoaded", () => {
    const body = document.body;
    const toggle = document.querySelector("[data-sidebar-toggle]");
    document.querySelector("[data-sidebar-dismiss]")?.addEventListener("click", () => body.classList.remove("sidebar-open"));
    toggle?.addEventListener("click", () => { const open = body.classList.toggle("sidebar-open"); toggle.setAttribute("aria-expanded", String(open)); });
    const form = document.querySelector("[data-order-form]");
    if (!form) return;
    const container = form.querySelector("[data-order-items]");
    const template = form.querySelector("[data-order-item-template]");
    const storeSelect = form.querySelector("[data-store-select]");
    const totalNode = form.querySelector("[data-order-total]");
    const inventory = JSON.parse(form.dataset.inventory || "{}");
    const currency = value => `EGP ${Number(value || 0).toLocaleString("en-US", {minimumFractionDigits: 2, maximumFractionDigits: 2})}`;
    function update() {
        let total = 0;
        container.querySelectorAll("[data-order-item-row]").forEach(row => {
            const product = row.querySelector("[data-product-select]");
            const option = product.selectedOptions[0];
            const tier = row.querySelector("[data-price-tier]").value;
            const quantity = Number(row.querySelector("[data-order-quantity]").value);
            const stock = Number(inventory[`${storeSelect.value}:${product.value}`] || 0);
            const priceKey = tier === "semi_wholesale" ? "semiWholesale" : tier;
            const price = Number(option?.dataset[priceKey] || 0);
            row.querySelector("[data-stock-preview]").textContent = product.value && storeSelect.value ? String(stock) : "-";
            row.querySelector("[data-price-preview]").textContent = product.value ? currency(price) : "-";
            const subtotal = quantity > 0 ? price * quantity : 0;
            row.querySelector("[data-subtotal-preview]").textContent = currency(subtotal);
            row.querySelector("[data-stock-warning]").textContent = quantity > stock && product.value && storeSelect.value ? `Only ${stock} available in this store.` : "";
            total += subtotal;
        });
        totalNode.textContent = currency(total);
    }
    form.addEventListener("input", update);
    form.addEventListener("change", update);
    form.addEventListener("submit", event => {
        if (form.dataset.submitting === "true") {
            event.preventDefault();
            return;
        }
        form.dataset.submitting = "true";
        const submit = event.submitter || form.querySelector("[data-order-submit]");
        if (submit) {
            submit.disabled = true;
            submit.textContent = "Creating order...";
        }
    });
    form.querySelector("[data-add-order-item]")?.addEventListener("click", () => { container.append(template.content.cloneNode(true)); update(); });
    container.addEventListener("click", event => { const button = event.target.closest("[data-remove-order-item]"); if (!button) return; if (container.querySelectorAll("[data-order-item-row]").length > 1) button.closest("[data-order-item-row]").remove(); update(); });
    update();
});
