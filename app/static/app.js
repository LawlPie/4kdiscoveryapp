/* 4K Discovery — minimal vanilla JS for async interactivity (no framework). */

/** Show a transient toast message in the bottom-right corner. */
function showToast(message, isError = false) {
    const el = document.getElementById("toast");
    if (!el) return;
    el.textContent = message;
    el.classList.remove("hidden");
    el.classList.toggle("ring-rose-500", isError);
    el.classList.toggle("ring-slate-700", !isError);
    clearTimeout(el._timer);
    el._timer = setTimeout(() => el.classList.add("hidden"), 2500);
}

/**
 * Toggle a product's favourite state via the JSON API and update the heart
 * icon in place — no full page reload.
 */
async function toggleFavorite(productId, btn) {
    btn.disabled = true;
    try {
        const resp = await fetch(`/api/favorite/${encodeURIComponent(productId)}`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
        });
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const data = await resp.json();

        const icon = btn.querySelector(".heart-icon");
        if (icon) icon.textContent = data.is_favorited ? "❤️" : "🤍";
        btn.dataset.favorited = data.is_favorited ? "true" : "false";

        showToast(data.is_favorited ? "Added to watchlist ❤️" : "Removed from watchlist");

        // On the watchlist page, an un-favourited card should disappear.
        if (!data.is_favorited && window.location.pathname.startsWith("/watchlist")) {
            const card = document.getElementById(`card-${productId}`);
            if (card) {
                card.style.transition = "opacity .25s, transform .25s";
                card.style.opacity = "0";
                card.style.transform = "scale(.95)";
                setTimeout(() => card.remove(), 250);
            }
        }
    } catch (err) {
        console.error(err);
        showToast("Could not update favourite", true);
    } finally {
        btn.disabled = false;
    }
}

/** Manually trigger a scrape run from the UI. */
async function triggerScrape(btn) {
    const original = btn.textContent;
    btn.disabled = true;
    btn.textContent = "⏳ Scraping…";
    try {
        const resp = await fetch("/api/scrape", { method: "POST" });
        const data = await resp.json();
        showToast(data.message || "Scrape started");
        // Reload after a short delay so fresh results appear.
        setTimeout(() => window.location.reload(), 4000);
    } catch (err) {
        console.error(err);
        showToast("Failed to start scrape", true);
        btn.disabled = false;
        btn.textContent = original;
    }
}
