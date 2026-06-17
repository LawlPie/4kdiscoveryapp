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

/** Smoothly fade out and remove a product card (when it leaves the view). */
function removeCard(productId) {
    const card = document.getElementById(`card-${productId}`);
    if (!card) return;
    card.style.transition = "opacity .25s, transform .25s";
    card.style.opacity = "0";
    card.style.transform = "scale(.95)";
    setTimeout(() => card.remove(), 250);
}

/** Update the heart button's icon/state within a card. */
function syncHeart(productId, favorited) {
    const card = document.getElementById(`card-${productId}`);
    const btn = card && card.querySelector(".heart-btn");
    if (!btn) return;
    const icon = btn.querySelector(".heart-icon");
    if (icon) icon.textContent = favorited ? "❤️" : "🤍";
    btn.dataset.favorited = favorited ? "true" : "false";
}

/** Update the owned button's icon/colour within a card. */
function syncOwned(productId, owned) {
    const card = document.getElementById(`card-${productId}`);
    const btn = card && card.querySelector(".owned-btn");
    if (!btn) return;
    btn.dataset.owned = owned ? "true" : "false";
    btn.classList.toggle("bg-emerald-500", owned);
    btn.classList.toggle("text-emerald-950", owned);
    btn.classList.toggle("bg-slate-950/70", !owned);
    btn.classList.toggle("text-slate-300", !owned);
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

        syncHeart(productId, data.is_favorited);
        // Favouriting clears 'owned' — keep the owned button in sync.
        syncOwned(productId, data.is_owned);

        showToast(data.is_favorited ? "Added to watchlist ❤️" : "Removed from watchlist");

        // On the watchlist page, an un-favourited card should disappear.
        if (!data.is_favorited && window.location.pathname.startsWith("/watchlist")) {
            removeCard(productId);
        }
    } catch (err) {
        console.error(err);
        showToast("Could not update favourite", true);
    } finally {
        btn.disabled = false;
    }
}

/**
 * Toggle a product's owned/collection state. Owned items are hidden from the
 * Trawler and Watchlist, so the card is removed there when marked owned (and
 * removed from the Collection view when un-marked).
 */
async function toggleOwned(productId, btn) {
    btn.disabled = true;
    try {
        const resp = await fetch(`/api/owned/${encodeURIComponent(productId)}`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
        });
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const data = await resp.json();

        syncOwned(productId, data.is_owned);
        // Marking owned clears the favourite flag.
        syncHeart(productId, data.is_favorited);

        showToast(data.is_owned ? "Added to your collection ✓" : "Removed from collection");

        const path = window.location.pathname;
        const onCollection = path.startsWith("/owned");
        // Disappear from deal/wishlist views when owned; from collection when not.
        if ((data.is_owned && !onCollection) || (!data.is_owned && onCollection)) {
            removeCard(productId);
        }
    } catch (err) {
        console.error(err);
        showToast("Could not update collection", true);
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
