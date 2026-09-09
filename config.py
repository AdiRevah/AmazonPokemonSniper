from __future__ import annotations

APP_NAME = "Amazon Pokemon Sniper"
VERSION = "1.0.0"

SELECTORS = {
    "product_title": "#productTitle",
    "price": "#corePrice_desktop .a-price, #buyBox .a-price, .a-price.aok-align-center, #price_inside_buybox, #newBuyBoxPrice, .a-price .a-offscreen",
    "add_to_cart": "#add-to-cart-button, #add-to-cart-button-ubb, #desktop_qualifiedBuyBox [id^='add-to-cart-button'], #buybox [id^='add-to-cart-button'], #exports_desktop_qualifiedBuybox_add_to_cart_feature_div input[name='submit.add-to-cart'], #addToCart_feature_div input[name='submit.add-to-cart'], input[name='submit.add-to-cart'], input[name='submit.addToCart'], [id='submit.add-to-cart'] input, [id='submit.addToCart'] input",
    "buy_now": "#buy-now-button, #desktop_qualifiedBuyBox [id^='buy-now-button'], #buybox [id^='buy-now-button'], #buyNow input[name='submit.buy-now'], input[name='submit.buy-now']",
    "buying_options": "a.a-button-text[title='See All Buying Options'], #buybox-see-all-buying-options a, #buybox-see-all-buying-options-announce, #buybox-see-all-buying-choices-announce, #aod-ingress-link, #aod-ingress-link-announce, a:has-text('See All Buying Options'), button:has-text('See All Buying Options'), [aria-label*='See All Buying Options'], a[href*='showOffers'], a[href*='aod'], a[href*='offer-listing'], a[href*='/gp/offer-listing/']",
    "side_panel_atc": "#all-offers-display input[name='submit.addToCart'], #aod-offer-list input[name='submit.addToCart'], #aod-pinned-offer input[name='submit.addToCart'], #all-offers-display-scroller input[name='submit.addToCart'], #all-offers-display-scroller input.a-button-input, #all-offers-display button:has-text('Add to Cart'), #all-offers-display-scroller button:has-text('Add to Cart'), [aria-label*='Add to Cart from seller'], [data-cy='aod-offer'] button:has-text('Add to Cart'), [data-testid*='offer'] button:has-text('Add to Cart')",
    "buying_options_more": "button:has-text('See more options'), [aria-label*='See more options'], [aria-label*='See more options on Amazon']",
    "proceed_to_checkout": "input[name='proceedToRetailCheckout'], #sc-buy-box-ptc-button, #attach-sidesheet-checkout-button, #attach-sidesheet-checkout-button-deliv, input[value='Proceed to checkout']",
    "place_order": "#placeYourOrder, input[name='placeYourOrder1'], input[name='placeYourOrder'], [data-action='place-your-order'] input",
    "address_select": "[id='shipToThisAddressButton'] input, input[value*='Use this address'], input[value*='Deliver to this address'], [data-testid='Address_selectShipToThisAddressPrompt']",
    "payment_select": "input[aria-label*='Use this payment method'], input[value*='Use this payment method'], [id='paymentContinueButton'] input, input[name='ppw-widgetState']",
    "continue_button": "input[value='Continue'], [data-action='page-spinner-continue-action'] input, #checkout-primary-continue-button-id input",
    "prime_decline": "#prime-decline-button, a[id='prime-decline-button'], a[href*='action=decline']",
    "cart_count": "#nav-cart-count",
    "oos_indicator": "#outOfStock, #availability, #outOfStock_feature_div",
    "captcha_check": "form[action='/errors/validateCaptcha'], #captchacharacters",
}

SITES = {
    "amazon_us": {
        "label": "Amazon US",
        "base_url": "https://www.amazon.com",
        "cart_url": "https://www.amazon.com/gp/cart/view.html",
    }
}
