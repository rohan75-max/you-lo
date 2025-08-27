import os
import math
import uuid
import datetime
import json
from datetime import timedelta
from functools import wraps
import sys
import csv
from io import StringIO
import base64
from PIL import Image
from slugify import slugify
import bleach
from flask import Flask, request, render_template, session, redirect, url_for, send_from_directory, jsonify, abort, make_response
import pymongo
from bson.objectid import ObjectId
from bson.errors import InvalidId
from jinja2 import Environment, DictLoader
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'change_me')

# MongoDB connection
mongo_uri = os.environ.get('MONGO_URI')
if not mongo_uri:
    raise ValueError("MONGO_URI not set")
client = pymongo.MongoClient(mongo_uri)
db = client.get_default_database()

# Collections
products_col = db.products
orders_col = db.orders
coupons_col = db.coupons
settings_col = db.settings
reviews_col = db.reviews
counters_col = db.counters

# Ensure indexes
products_col.create_index('slug')
products_col.create_index('status')
products_col.create_index('tags')
orders_col.create_index('order_id')

# Asset dir
ASSET_DIR = os.environ.get('ASSET_DIR', 'uploads')
os.makedirs(ASSET_DIR, exist_ok=True)

ADMIN_USER = os.environ.get('ADMIN_USER', 'admin')
ADMIN_PASS = os.environ.get('ADMIN_PASS', 'admin123')
SITE_NAME = os.environ.get('SITE_NAME', 'TeeLux')
BASE_URL = os.environ.get('BASE_URL', 'http://localhost:5000')

# Helpers
def format_money(amount, currency='BDT'):
    return f"{amount:,.2f} {currency}"

def generate_slug(name):
    return slugify(name)

def generate_sku(product_name, color, size):
    return f"{slugify(product_name)}-{slugify(color)}-{size}".upper()

def get_next_order_id():
    counter = counters_col.find_one_and_update(
        {'_id': 'order_id'},
        {'$inc': {'seq': 1}},
        upsert=True,
        return_document=pymongo.ReturnDocument.AFTER
    )
    return counter['seq']

def make_thumbnail(file_path, size=(200, 200)):
    try:
        img = Image.open(file_path)
        img.thumbnail(size)
        thumb_name = 'thumb_' + os.path.basename(file_path)
        thumb_path = os.path.join(ASSET_DIR, thumb_name)
        img.save(thumb_path, quality=85)
        return thumb_name
    except:
        return None

def upload_image(file, compress=True):
    if not file:
        return None
    filename = secure_filename(file.filename)
    if not filename.lower().endswith(('.png', '.jpg', '.jpeg')):
        return None
    unique_name = str(uuid.uuid4()) + '_' + filename
    path = os.path.join(ASSET_DIR, unique_name)
    file.save(path)
    if compress:
        try:
            img = Image.open(path)
            img = img.convert('RGB')
            img.save(path, 'JPEG', quality=85)
        except:
            pass
    # Note: For Heroku ephemeral storage, consider S3 integration here.
    # Example: boto3.client('s3').upload_file(path, 'bucket', unique_name)
    return unique_name

def clean_html(text):
    allowed_tags = ['p', 'b', 'i', 'u', 'ul', 'ol', 'li', 'a', 'br']
    allowed_attrs = {'a': ['href']}
    return bleach.clean(text, tags=allowed_tags, attributes=allowed_attrs)

def generate_csrf():
    if 'csrf_token' not in session:
        session['csrf_token'] = str(uuid.uuid4())
    return session['csrf_token']

def check_csrf():
    if request.form.get('csrf_token') != session.get('csrf_token'):
        abort(403)

def get_settings():
    settings = settings_col.find_one({'_id': 'main'})
    if not settings:
        settings = {
            '_id': 'main',
            'brand': SITE_NAME,
            'support_phone': '1234567890',
            'support_email': 'support@teelux.com',
            'bkash_number': '01xxxxxxxxx',
            'nagad_number': '01xxxxxxxxx',
            'verification_sla': '24',
            'banners': [],
            'seo_title': SITE_NAME,
            'seo_desc': 'Premium T-Shirts',
            'seo_og_image': '',
            'maintenance': False,
            'shipping_methods': [
                {'name': 'Standard', 'fee': 50.0, 'desc': '3-5 days'},
                {'name': 'Express', 'fee': 100.0, 'desc': '1-2 days'}
            ],
            'free_shipping_threshold': 1000.0
        }
        settings_col.insert_one(settings)
    return settings

# Admin required decorator
def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'admin_logged_in' not in session:
            return redirect(url_for('admin_login'))
        return f(*args, **kwargs)
    return decorated_function

# Rate limiter simulation (basic IP-based)
request_counts = {}
def rate_limit():
    ip = request.remote_addr
    now = datetime.datetime.utcnow()
    window = now - timedelta(minutes=1)
    request_counts[ip] = [t for t in request_counts.get(ip, []) if t > window]
    if len(request_counts[ip]) >= 10:
        abort(429)
    request_counts[ip].append(now)

# Templates
templates = {
    'base.html': '''
<!doctype html>
<html lang="en" x-data="{ darkMode: localStorage.theme === 'dark' || (!('theme' in localStorage) && window.matchMedia('(prefers-color-scheme: dark)').matches), toggleDarkMode() { this.darkMode = !this.darkMode; localStorage.theme = this.darkMode ? 'dark' : 'light'; } }" :class="darkMode ? 'dark' : ''">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{{ title | default(settings.brand ~ ' - Premium T-Shirts') }}</title>
    <meta name="description" content="{{ settings.seo_desc }}">
    <meta property="og:image" content="{{ settings.seo_og_image }}">
    <script src="https://cdn.tailwindcss.com"></script>
    <script src="https://unpkg.com/htmx.org@1.9.6"></script>
    <script src="https://cdn.jsdelivr.net/npm/alpinejs@3.x.x/dist/cdn.min.js" defer></script>
    <script src="https://unpkg.com/lucide@latest"></script>
    <style>
        body { font-family: system-ui, -apple-system, sans-serif; }
        .swatch { width: 24px; height: 24px; border-radius: 50%; border: 1px solid #ccc; cursor: pointer; }
        .toast { transition: opacity 0.5s; }
        [x-cloak] { display: none; }
    </style>
</head>
<body class="bg-gray-50 dark:bg-gray-900 text-gray-900 dark:text-gray-100" x-init="lucide.createIcons()">
    <header class="sticky top-0 bg-white dark:bg-gray-800 shadow z-10">
        <nav class="container mx-auto px-4 py-4 flex items-center justify-between">
            <a href="/" class="text-2xl font-bold">{{ settings.brand }}</a>
            <div class="flex items-center space-x-4">
                <form action="/" class="relative">
                    <input name="search" placeholder="Search..." class="rounded-full px-4 py-2 bg-gray-100 dark:bg-gray-700">
                    <i data-lucide="search" class="absolute right-3 top-2.5"></i>
                </form>
                <a href="/cart" class="relative">
                    <i data-lucide="shopping-cart"></i>
                    <span id="cart-badge" class="absolute -top-2 -right-2 bg-red-500 text-white rounded-full px-2 text-xs">{{ session.cart | length if session.cart else 0 }}</span>
                </a>
                <button @click="toggleDarkMode()">
                    <i data-lucide="moon" x-show="darkMode"></i>
                    <i data-lucide="sun" x-show="!darkMode"></i>
                </button>
            </div>
        </nav>
    </header>
    <main class="container mx-auto px-4 py-8">
        {% block content %}{% endblock %}
    </main>
    <footer class="bg-gray-100 dark:bg-gray-800 py-4 text-center">
        <p>Contact: {{ settings.support_email }} | {{ settings.support_phone }}</p>
        <p>&copy; {{ settings.brand }} {{ 'now' | date('YYYY') }}. All rights reserved.</p>
    </footer>
    <div id="toast-container" class="fixed bottom-4 right-4 space-y-2"></div>
    <script>
        function showToast(message, type = 'success') {
            const toast = document.createElement('div');
            toast.className = `toast p-4 rounded-2xl shadow ${type === 'success' ? 'bg-green-500' : 'bg-red-500'} text-white`;
            toast.textContent = message;
            document.getElementById('toast-container').appendChild(toast);
            setTimeout(() => toast.remove(), 3000);
        }
    </script>
</body>
</html>
    ''',

    'maintenance.html': '''
{% extends "base.html" %}
{% block content %}
<div class="text-center py-20">
    <h1 class="text-4xl font-bold">Site Under Maintenance</h1>
    <p>We'll be back soon!</p>
</div>
{% endblock %}
    ''',

    'home.html': '''
{% extends "base.html" %}
{% block content %}
<div class="mb-6 flex flex-wrap gap-4">
    <form action="/" class="flex gap-2 flex-wrap">
        <select name="category">
            <option value="">All Categories</option>
            {% for cat in categories %}
            <option value="{{ cat }}" {{ 'selected' if cat == request.args.get('category') }}>{{ cat }}</option>
            {% endfor %}
        </select>
        <select name="color">
            <option value="">All Colors</option>
            {% for color in colors %}
            <option value="{{ color }}" {{ 'selected' if color == request.args.get('color') }}>{{ color }}</option>
            {% endfor %}
        </select>
        <select name="size">
            <option value="">All Sizes</option>
            {% for size in sizes %}
            <option value="{{ size }}" {{ 'selected' if size == request.args.get('size') }}>{{ size }}</option>
            {% endfor %}
        </select>
        <input type="number" name="min_price" placeholder="Min Price" value="{{ request.args.get('min_price') }}" class="rounded px-2 py-1 bg-gray-100 dark:bg-gray-700">
        <input type="number" name="max_price" placeholder="Max Price" value="{{ request.args.get('max_price') }}" class="rounded px-2 py-1 bg-gray-100 dark:bg-gray-700">
        <label class="flex items-center"><input type="checkbox" name="in_stock" {{ 'checked' if request.args.get('in_stock') }} class="mr-2"> In Stock</label>
        <button type="submit" class="bg-blue-500 text-white px-4 py-2 rounded-2xl hover:bg-blue-600">Filter</button>
    </form>
</div>
<div class="grid grid-cols-2 md:grid-cols-4 lg:grid-cols-5 gap-4">
    {% for product in products %}
    <div class="rounded-2xl shadow hover:shadow-lg transition bg-white dark:bg-gray-800">
        <a href="/p/{{ product.slug }}">
            <img src="{{ product.images[0] if product.images[0].startswith('http') else '/uploads/' + (make_thumbnail(product.images[0]) or product.images[0]) }}" class=" rounded-t-2xl w-full h-48 object-cover">
        </a>
        <div class="p-4">
            <h3 class="font-bold">{{ product.name }}</h3>
            <p>{{ format_money(product.price) }}{% if product.compare_at_price %} <span class="line-through text-gray-500">{{ format_money(product.compare_at_price) }}</span>{% endif %}</p>
            <p class="text-sm">{{ product.description | truncate(50) | striptags }}</p>
            <div class="flex items-center">
                {% for i in range(5) %}
                <i data-lucide="{{ 'star' if i < product.avg_rating | int else 'star-off' }}" class="w-4 h-4"></i>
                {% endfor %}
            </div>
            {% if product.tags %}
            <div class="mt-2">
                {% for tag in product.tags %}
                <span class="inline-block bg-blue-100 text-blue-800 text-xs px-2 rounded-full">{{ tag }}</span>
                {% endfor %}
            </div>
            {% endif %}
            <button hx-post="/cart/add" hx-vals='{"product_id": "{{ product._id }}", "quantity": 1}' hx-target="#cart-badge" hx-swap="innerHTML" class="mt-2 bg-blue-500 text-white px-4 py-2 rounded-2xl hover:bg-blue-600">Add to Cart</button>
        </div>
    </div>
    {% endfor %}
</div>
<div class="flex justify-center mt-6 gap-2">
    {% if page > 1 %}<a href="?page={{ page - 1 }}&{{ query_string }}" class="px-4 py-2 bg-gray-200 rounded-2xl hover:bg-gray-300">Prev</a>{% endif %}
    {% if page < total_pages %}<a href="?page={{ page + 1 }}&{{ query_string }}" class="px-4 py-2 bg-gray-200 rounded-2xl hover:bg-gray-300">Next</a>{% endif %}
</div>
{% endblock %}
    ''',

    'product_detail.html': '''
{% extends "base.html" %}
{% block content %}
<div class="flex flex-col md:flex-row gap-8">
    <div class="flex-1">
        <img id="main-image" src="{{ product.images[0] if product.images[0].startswith('http') else '/uploads/' + product.images[0] }}" class="w-full h-96 object-cover rounded-2xl">
        <div class="grid grid-cols-4 gap-2 mt-2">
            {% for img in product.images %}
            <img src="{{ img if img.startswith('http') else '/uploads/' + (make_thumbnail(img) or img) }}" class="cursor-pointer rounded hover:opacity-75" @click="document.getElementById('main-image').src = this.src">
            {% endfor %}
        </div>
    </div>
    <div class="flex-1">
        <h1 class="text-3xl font-bold">{{ product.name }}</h1>
        <p class="text-2xl">{{ format_money(product.price) }}{% if product.compare_at_price %} <span class="line-through text-gray-500">{{ format_money(product.compare_at_price) }}</span>{% endif %}</p>
        <div x-data="{ selectedColor: '{{ product.variants[0].color if product.variants else '' }}', selectedSize: '{{ product.variants[0].size if product.variants else '' }}', stock: 0 }" x-init="updateStock()">
            <div class="mt-4">
                <label class="block font-semibold">Color:</label>
                {% for color in colors %}
                <div class="swatch inline-block mr-2" style="background-color: {{ color }};" @click="selectedColor = '{{ color }}'; updateStock()"></div>
                {% endfor %}
            </div>
            <div class="mt-2">
                <label class="block font-semibold">Size:</label>
                <select x-model="selectedSize" @change="updateStock()" class="rounded px-2 py-1 bg-gray-100 dark:bg-gray-700">
                    {% for size in sizes %}
                    <option>{{ size }}</option>
                    {% endfor %}
                </select>
            </div>
            <p class="mt-2">Stock: <span x-text="stock"></span></p>
            <p class="mt-2">SKU: <span x-text="getSKU()"></span></p>
            <form hx-post="/cart/add" class="mt-4" @submit="if(!selectedColor || !selectedSize) { showToast('Please select color and size', 'error'); return false; }">
                <input type="hidden" name="product_id" value="{{ product._id }}">
                <input type="hidden" name="color" x-model="selectedColor">
                <input type="hidden" name="size" x-model="selectedSize">
                <input type="hidden" name="csrf_token" value="{{ generate_csrf() }}">
                <input type="number" name="quantity" value="1" min="1" class="w-16 rounded px-2 py-1 bg-gray-100 dark:bg-gray-700">
                <button type="submit" class="bg-blue-500 text-white px-4 py-2 rounded-2xl hover:bg-blue-600">Add to Cart</button>
            </form>
            <script>
                function updateStock() {
                    const variants = {{ product.variants | tojson }};
                    const variant = variants.find(v => v.color === this.selectedColor && v.size === this.selectedSize);
                    this.stock = variant ? variant.stock : 0;
                }
                function getSKU() {
                    const variants = {{ product.variants | tojson }};
                    const variant = variants.find(v => v.color === this.selectedColor && v.size === this.selectedSize);
                    return variant ? variant.sku : '';
                }
            </script>
        </div>
        <div class="mt-4" x-data="{ open: false }">
            <button @click="open = true" class="text-blue-500 hover:underline">Size Chart</button>
            <div x-show="open" x-cloak class="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center">
                <div class="bg-white dark:bg-gray-800 p-6 rounded-2xl">
                    <h3 class="text-xl font-bold">Size Chart</h3>
                    <table class="mt-2">
                        <tr><th>Size</th><th>Chest</th><th>Length</th></tr>
                        <tr><td>XS</td><td>32-34"</td><td>25"</td></tr>
                        <tr><td>S</td><td>35-37"</td><td>26"</td></tr>
                        <tr><td>M</td><td>38-40"</td><td>27"</td></tr>
                        <tr><td>L</td><td>41-43"</td><td>28"</td></tr>
                        <tr><td>XL</td><td>44-46"</td><td>29"</td></tr>
                        <tr><td>XXL</td><td>47-49"</td><td>30"</td></tr>
                    </table>
                    <button @click="open = false" class="mt-4 bg-gray-500 text-white px-4 py-2 rounded-2xl">Close</button>
                </div>
            </div>
        </div>
        <div class="mt-4 prose dark:prose-invert">{{ product.description | safe }}</div>
        <div class="mt-4">
            <h3 class="font-semibold">Materials & Care</h3>
            <p>100% Cotton. Machine wash cold, tumble dry low.</p>
        </div>
    </div>
</div>
<div class="mt-8">
    <h2 class="text-2xl font-bold">Reviews</h2>
    {% if reviews %}
    {% for review in reviews %}
    <div class="border-b py-4">
        <p class="font-semibold">{{ review.name }} - {{ review.rating }} stars</p>
        <p>{{ review.comment }}</p>
        <p class="text-sm text-gray-500">{{ review.created_at | date('YYYY-MM-DD') }}</p>
    </div>
    {% endfor %}
    {% else %}
    <p>No reviews yet.</p>
    {% endif %}
    <form method="post" action="/review/add/{{ product._id }}" class="mt-4" hx-post="/review/add/{{ product._id }}" hx-swap="none" @submit="if(!this.name.value || !this.comment.value) { showToast('Name and comment required', 'error'); return false; }">
        <input type="hidden" name="csrf_token" value="{{ generate_csrf() }}">
        <input name="name" placeholder="Your Name" class="rounded px-2 py-1 bg-gray-100 dark:bg-gray-700 w-full mb-2">
        <select name="rating" class="rounded px-2 py-1 bg-gray-100 dark:bg-gray-700 mb-2">
            <option value="5">5 Stars</option>
            <option value="4">4 Stars</option>
            <option value="3">3 Stars</option>
            <option value="2">2 Stars</option>
            <option value="1">1 Star</option>
        </select>
        <textarea name="comment" placeholder="Your Review" class="rounded px-2 py-1 bg-gray-100 dark:bg-gray-700 w-full mb-2"></textarea>
        <input type="hidden" name="slug" value="{{ product.slug }}">
        <button type="submit" class="bg-blue-500 text-white px-4 py-2 rounded-2xl hover:bg-blue-600">Submit Review</button>
    </form>
</div>
{% endblock %}
    ''',

    'cart.html': '''
{% extends "base.html" %}
{% block content %}
<h1 class="text-3xl font-bold">Your Cart</h1>
{% if cart %}
<table class="w-full mt-4 border-collapse">
    <thead>
        <tr class="bg-gray-100 dark:bg-gray-700">
            <th class="p-2 text-left">Product</th>
            <th class="p-2 text-left">Variant</th>
            <th class="p-2 text-left">Quantity</th>
            <th class="p-2 text-left">Price</th>
            <th class="p-2 text-left">Subtotal</th>
            <th></th>
        </tr>
    </thead>
    <tbody>
        {% for item in cart %}
        <tr class="border-b">
            <td class="p-2">{{ item.product.name }}</td>
            <td class="p-2">{{ item.variant.color }} / {{ item.variant.size }}</td>
            <td class="p-2">
                <form hx-post="/cart/update/{{ loop.index0 }}" hx-swap="outerHTML">
                    <input type="hidden" name="csrf_token" value="{{ generate_csrf() }}">
                    <input type="number" name="quantity" value="{{ item.qty }}" min="1" class="w-16 rounded px-2 py-1 bg-gray-100 dark:bg-gray-700" hx-trigger="change">
                </form>
            </td>
            <td class="p-2">{{ format_money(item.price) }}</td>
            <td class="p-2">{{ format_money(item.price * item.qty) }}</td>
            <td class="p-2">
                <button hx-post="/cart/remove/{{ loop.index0 }}" hx-swap="none" class="text-red-500 hover:underline">Remove</button>
            </td>
        </tr>
        {% endfor %}
    </tbody>
</table>
<div class="mt-4">
    <form hx-post="/cart/apply_coupon" class="flex gap-2">
        <input type="hidden" name="csrf_token" value="{{ generate_csrf() }}">
        <input name="code" placeholder="Coupon Code" class="rounded px-2 py-1 bg-gray-100 dark:bg-gray-700">
        <button class="bg-blue-500 text-white px-4 py-2 rounded-2xl hover:bg-blue-600">Apply</button>
    </form>
    {% if coupon_error %}<p class="text-red-500 mt-2">{{ coupon_error }}</p>{% endif %}
    {% if coupon_applied %}<p class="text-green-500 mt-2">Coupon {{ coupon_applied }} applied</p>{% endif %}
</div>
<div class="mt-4">
    <p>Subtotal: {{ format_money(subtotal) }}</p>
    {% if discount > 0 %}<p>Discount: {{ format_money(discount) }}</p>{% endif %}
    <p>Shipping: {{ format_shipping }}</p>
    <p class="text-xl font-bold">Total: {{ format_money(total) }}</p>
</div>
<a href="/checkout" class="inline-block mt-4 bg-green-500 text-white px-6 py-3 rounded-2xl hover:bg-green-600">Proceed to Checkout</a>
{% else %}
<p>Your cart is empty.</p>
<a href="/" class="inline-block mt-4 bg-blue-500 text-white px-4 py-2 rounded-2xl hover:bg-blue-600">Shop Now</a>
{% endif %}
{% endblock %}
    ''',

    'checkout.html': '''
{% extends "base.html" %}
{% block content %}
<h1 class="text-3xl font-bold">Checkout</h1>
<div class="flex flex-col md:flex-row gap-8 mt-4">
    <div class="flex-1">
        <form method="post" action="/payment" class="space-y-4">
            <input type="hidden" name="csrf_token" value="{{ generate_csrf() }}">
            <div>
                <label class="block font-semibold">Name</label>
                <input name="name" required class="w-full rounded px-2 py-1 bg-gray-100 dark:bg-gray-700">
            </div>
            <div>
                <label class="block font-semibold">Phone</label>
                <input name="phone" type="tel" required class="w-full rounded px-2 py-1 bg-gray-100 dark:bg-gray-700">
            </div>
            <div>
                <label class="block font-semibold">Email</label>
                <input name="email" type="email" required class="w-full rounded px-2 py-1 bg-gray-100 dark:bg-gray-700">
            </div>
            <div>
                <label class="block font-semibold">Address</label>
                <textarea name="address" required class="w-full rounded px-2 py-1 bg-gray-100 dark:bg-gray-700"></textarea>
            </div>
            <div>
                <label class="block font-semibold">City</label>
                <input name="city" required class="w-full rounded px-2 py-1 bg-gray-100 dark:bg-gray-700">
            </div>
            <div>
                <label class="block font-semibold">Postal Code</label>
                <input name="postal_code" required class="w-full rounded px-2 py-1 bg-gray-100 dark:bg-gray-700">
            </div>
            <div>
                <label class="block font-semibold">Delivery Note</label>
                <textarea name="delivery_note" class="w-full rounded px-2 py-1 bg-gray-100 dark:bg-gray-700"></textarea>
            </div>
            <div>
                <label class="block font-semibold">Shipping Method</label>
                <select name="shipping_method" required class="w-full rounded px-2 py-1 bg-gray-100 dark:bg-gray-700">
                    {% for method in settings.shipping_methods %}
                    <option value="{{ method.name }}">{{ method.name }} ({{ format_money(method.fee) }}) - {{ method.desc }}</option>
                    {% endfor %}
                </select>
            </div>
            <button type="submit" class="bg-green-500 text-white px-6 py-3 rounded-2xl hover:bg-green-600">Proceed to Payment</button>
        </form>
    </div>
    <div class="flex-1">
        <h2 class="text-xl font-bold">Order Summary</h2>
        <table class="w-full mt-2">
            {% for item in cart %}
            <tr>
                <td>{{ item.product.name }} ({{ item.variant.color }} / {{ item.variant.size }})</td>
                <td>x{{ item.qty }}</td>
                <td>{{ format_money(item.price * item.qty) }}</td>
            </tr>
            {% endfor %}
        </table>
        <p class="mt-2">Subtotal: {{ format_money(subtotal) }}</p>
        {% if discount > 0 %}<p>Discount: {{ format_money(discount) }}</p>{% endif %}
        <p>Shipping: {{ format_shipping }}</p>
        <p class="text-xl font-bold">Total: {{ format_money(total) }}</p>
    </div>
</div>
{% endblock %}
    ''',

    'payment.html': '''
{% extends "base.html" %}
{% block content %}
<h1 class="text-3xl font-bold">Payment</h1>
<div class="flex flex-col md:flex-row gap-8 mt-4">
    <div class="flex-1">
        <h2 class="text-xl font-bold">Payment Instructions</h2>
        <div class="mt-2">
            <h3 class="font-semibold">bKash</h3>
            <p>Send payment to: {{ settings.bkash_number }}</p>
            <p>Steps: Open bKash app, select Send Money, enter number, amount, and your PIN.</p>
        </div>
        <div class="mt-2">
            <h3 class="font-semibold">Nagad</h3>
            <p>Send payment to: {{ settings.nagad_number }}</p>
            <p>Steps: Open Nagad app, select Send Money, enter number, amount, and your PIN.</p>
        </div>
        <form method="post" enctype="multipart/form-data" class="mt-4 space-y-4">
            <input type="hidden" name="csrf_token" value="{{ generate_csrf() }}">
            <div>
                <label class="block font-semibold">Payment Method</label>
                <select name="method" required class="w-full rounded px-2 py-1 bg-gray-100 dark:bg-gray-700">
                    <option value="bkash">bKash</option>
                    <option value="nagad">Nagad</option>
                </select>
            </div>
            <div>
                <label class="block font-semibold">Transaction ID (TRX/TxnID)</label>
                <input name="trx_id" required class="w-full rounded px-2 py-1 bg-gray-100 dark:bg-gray-700">
            </div>
            <div>
                <label class="block font-semibold">Payment Screenshot</label>
                <input type="file" name="screenshot" accept="image/*" required class="mt-1">
            </div>
            <button type="submit" class="bg-green-500 text-white px-6 py-3 rounded-2xl hover:bg-green-600">Submit Payment</button>
        </form>
    </div>
    <div class="flex-1">
        <h2 class="text-xl font-bold">Order Summary</h2>
        <table class="w-full mt-2">
            {% for item in cart %}
            <tr>
                <td>{{ item.product.name }} ({{ item.variant.color }} / {{ item.variant.size }})</td>
                <td>x{{ item.qty }}</td>
                <td>{{ format_money(item.price * item.qty) }}</td>
            </tr>
            {% endfor %}
        </table>
        <p class="mt-2">Subtotal: {{ format_money(subtotal) }}</p>
        {% if discount > 0 %}<p>Discount: {{ format_money(discount) }}</p>{% endif %}
        <p>Shipping: {{ format_shipping }}</p>
        <p class="text-xl font-bold">Total: {{ format_money(total) }}</p>
    </div>
</div>
{% endblock %}
    ''',

    'thank_you.html': '''
{% extends "base.html" %}
{% block content %}
<div class="text-center py-20">
    <h1 class="text-3xl font-bold">Thank You for Your Order!</h1>
    <p>Order ID: {{ order_id }}</p>
    <p>We'll verify your payment within {{ settings.verification_sla }} hours.</p>
    <p>Track your order <a href="/track" class="text-blue-500 hover:underline">here</a>.</p>
</div>
{% endblock %}
    ''',

    'track.html': '''
{% extends "base.html" %}
{% block content %}
<h1 class="text-3xl font-bold">Track Your Order</h1>
<form method="post" class="mt-4 space-y-4">
    <input type="hidden" name="csrf_token" value="{{ generate_csrf() }}">
    <div>
        <label class="block font-semibold">Order ID</label>
        <input name="order_id" required class="w-full rounded px-2 py-1 bg-gray-100 dark:bg-gray-700">
    </div>
    <div>
        <label class="block font-semibold">Phone or Email</label>
        <input name="contact" required class="w-full rounded px-2 py-1 bg-gray-100 dark:bg-gray-700">
    </div>
    <button type="submit" class="bg-blue-500 text-white px-4 py-2 rounded-2xl hover:bg-blue-600">Track</button>
</form>
{% if order %}
<div class="mt-6">
    <h2 class="text-xl font-bold">Order #{{ order.order_id }}</h2>
    <p>Status: {{ order.status | capitalize }}</p>
    <div class="mt-4">
        <h3 class="font-semibold">Timeline</h3>
        <ul class="space-y-2">
            <li>Pending Verification: {{ order.created_at | date('YYYY-MM-DD HH:mm') }}</li>
            {% if order.status != 'pending_verification' %}
            <li>Verified: {{ order.payment.verified_at | date('YYYY-MM-DD HH:mm') if order.payment.verified_at }}</li>
            {% endif %}
            {% if order.status in ['processing', 'shipped', 'delivered'] %}
            <li>Processing: {{ order.updated_at | date('YYYY-MM-DD HH:mm') }}</li>
            {% endif %}
            {% if order.status in ['shipped', 'delivered'] %}
            <li>Shipped: {{ order.updated_at | date('YYYY-MM-DD HH:mm') }}</li>
            {% endif %}
            {% if order.status == 'delivered' %}
            <li>Delivered: {{ order.updated_at | date('YYYY-MM-DD HH:mm') }}</li>
            {% endif %}
            {% if order.status in ['canceled', 'refunded'] %}
            <li>{{ order.status | capitalize }}: {{ order.updated_at | date('YYYY-MM-DD HH:mm') }}</li>
            {% endif %}
        </ul>
    </div>
    <div class="mt-4">
        <h3 class="font-semibold">Items</h3>
        <table class="w-full">
            {% for item in order.items %}
            <tr>
                <td>{{ item.product_id.name }} ({{ item.variant.color }} / {{ item.variant.size }})</td>
                <td>x{{ item.qty }}</td>
                <td>{{ format_money(item.price * item.qty) }}</td>
            </tr>
            {% endfor %}
        </table>
    </div>
    <div class="mt-4">
        <h3 class="font-semibold">Shipping</h3>
        <p>{{ order.shipping.address }}, {{ order.shipping.city }}, {{ order.shipping.postal_code }}</p>
    </div>
    <div class="mt-4">
        <h3 class="font-semibold">Payment</h3>
        <p>Method: {{ order.payment.method | capitalize }}</p>
        <p>Transaction ID: {{ order.payment.trx_id[-4:] | prepend('****') }}</p>
    </div>
</div>
{% endif %}
{% if error %}<p class="text-red-500 mt-4">{{ error }}</p>{% endif %}
{% endblock %}
    ''',

    'admin_login.html': '''
{% extends "base.html" %}
{% block content %}
<div class="max-w-md mx-auto mt-10">
    <h1 class="text-3xl font-bold">Admin Login</h1>
    <form method="post" class="mt-4 space-y-4">
        <input type="hidden" name="csrf_token" value="{{ generate_csrf() }}">
        <div>
            <label class="block font-semibold">Username</label>
            <input name="username" required class="w-full rounded px-2 py-1 bg-gray-100 dark:bg-gray-700">
        </div>
        <div>
            <label class="block font-semibold">Password</label>
            <input name="password" type="password" required class="w-full rounded px-2 py-1 bg-gray-100 dark:bg-gray-700">
        </div>
        <button type="submit" class="bg-blue-500 text-white px-4 py-2 rounded-2xl hover:bg-blue-600">Login</button>
    </form>
    {% if error %}<p class="text-red-500 mt-2">{{ error }}</p>{% endif %}
</div>
{% endblock %}
    ''',

    'admin_dashboard.html': '''
{% extends "base.html" %}
{% block content %}
<h1 class="text-3xl font-bold">Admin Dashboard</h1>
<div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4 mt-6">
    <div class="bg-white dark:bg-gray-800 p-4 rounded-2xl shadow">
        <h3 class="font-semibold">Total Products</h3>
        <p class="text-2xl">{{ total_products }}</p>
    </div>
    <div class="bg-white dark:bg-gray-800 p-4 rounded-2xl shadow">
        <h3 class="font-semibold">Total Orders</h3>
        <p class="text-2xl">{{ total_orders }}</p>
    </div>
    <div class="bg-white dark:bg-gray-800 p-4 rounded-2xl shadow">
        <h3 class="font-semibold">Pending Verifications</h3>
        <p class="text-2xl">{{ pending }}</p>
    </div>
    <div class="bg-white dark:bg-gray-800 p-4 rounded-2xl shadow">
        <h3 class="font-semibold">Revenue (Last 30 Days)</h3>
        <p class="text-2xl">{{ format_money(revenue) }}</p>
    </div>
</div>
<div class="mt-6 flex flex-wrap gap-4">
    <a href="/admin/products" class="inline-block bg-blue-500 text-white px-4 py-2 rounded-2xl hover:bg-blue-600">Manage Products</a>
    <a href="/admin/orders" class="inline-block bg-blue-500 text-white px-4 py-2 rounded-2xl hover:bg-blue-600">Manage Orders</a>
    <a href="/admin/payments" class="inline-block bg-blue-500 text-white px-4 py-2 rounded-2xl hover:bg-blue-600">Payment Verifications</a>
    <a href="/admin/coupons" class="inline-block bg-blue-500 text-white px-4 py-2 rounded-2xl hover:bg-blue-600">Manage Coupons</a>
    <a href="/admin/shipping" class="inline-block bg-blue-500 text-white px-4 py-2 rounded-2xl hover:bg-blue-600">Shipping Settings</a>
    <a href="/admin/settings" class="inline-block bg-blue-500 text-white px-4 py-2 rounded-2xl hover:bg-blue-600">Settings</a>
    <a href="/admin/users" class="inline-block bg-blue-500 text-white px-4 py-2 rounded-2xl hover:bg-blue-600">Users</a>
    <a href="/admin/seed" class="inline-block bg-yellow-500 text-white px-4 py-2 rounded-2xl hover:bg-yellow-600">Seed Data</a>
</div>
{% endblock %}
    ''',

    'admin_products.html': '''
{% extends "base.html" %}
{% block content %}
<h1 class="text-3xl font-bold">Manage Products</h1>
<a href="/admin/products/new" class="inline-block mt-4 bg-green-500 text-white px-4 py-2 rounded-2xl hover:bg-green-600">Add New Product</a>
<form action="/admin/products" class="mt-4 flex gap-2 flex-wrap">
    <input name="search" placeholder="Search products..." value="{{ request.args.get('search') }}" class="rounded px-2 py-1 bg-gray-100 dark:bg-gray-700">
    <select name="status" class="rounded px-2 py-1 bg-gray-100 dark:bg-gray-700">
        <option value="">All Status</option>
        <option value="active" {{ 'selected' if request.args.get('status') == 'active' }}>Active</option>
        <option value="draft" {{ 'selected' if request.args.get('status') == 'draft' }}>Draft</option>
    </select>
    <button type="submit" class="bg-blue-500 text-white px-4 py-2 rounded-2xl hover:bg-blue-600">Filter</button>
</form>
<table class="w-full mt-4 border-collapse">
    <thead>
        <tr class="bg-gray-100 dark:bg-gray-700">
            <th class="p-2 text-left">Name</th>
            <th class="p-2 text-left">Price</th>
            <th class="p-2 text-left">Status</th>
            <th class="p-2 text-left">Actions</th>
        </tr>
    </thead>
    <tbody>
        {% for product in products %}
        <tr class="border-b">
            <td class="p-2">{{ product.name }}</td>
            <td class="p-2">{{ format_money(product.price) }}</td>
            <td class="p-2">{{ product.status | capitalize }}</td>
            <td class="p-2">
                <a href="/admin/products/edit/{{ product._id }}" class="text-blue-500 hover:underline">Edit</a>
                <button hx-post="/admin/products/toggle/{{ product._id }}" hx-swap="none" class="text-blue-500 hover:underline ml-2">{{ 'Deactivate' if product.status == 'active' else 'Activate' }}</button>
                <button hx-post="/admin/products/delete/{{ product._id }}" hx-swap="none" hx-confirm="Are you sure?" class="text-red-500 hover:underline ml-2">Delete</button>
            </td>
        </tr>
        {% endfor %}
    </tbody>
</table>
<div class="flex justify-center mt-6 gap-2">
    {% if page > 1 %}<a href="?page={{ page - 1 }}&{{ query_string }}" class="px-4 py-2 bg-gray-200 rounded-2xl hover:bg-gray-300">Prev</a>{% endif %}
    {% if page < total_pages %}<a href="?page={{ page + 1 }}&{{ query_string }}" class="px-4 py-2 bg-gray-200 rounded-2xl hover:bg-gray-300">Next</a>{% endif %}
</div>
{% endblock %}
    ''',

    'admin_product_edit.html': '''
{% extends "base.html" %}
{% block content %}
<h1 class="text-3xl font-bold">{{ 'Edit Product' if product else 'New Product' }}</h1>
<form method="post" enctype="multipart/form-data" class="mt-4 space-y-4">
    <input type="hidden" name="csrf_token" value="{{ generate_csrf() }}">
    <div>
        <label class="block font-semibold">Name</label>
        <input name="name" value="{{ product.name if product }}" required class="w-full rounded px-2 py-1 bg-gray-100 dark:bg-gray-700">
    </div>
    <div>
        <label class="block font-semibold">Category</label>
        <input name="category" value="{{ product.category if product }}" class="w-full rounded px-2 py-1 bg-gray-100 dark:bg-gray-700">
    </div>
    <div>
        <label class="block font-semibold">Tags (comma-separated)</label>
        <input name="tags" value="{{ product.tags | join(', ') if product.tags else '' }}" class="w-full rounded px-2 py-1 bg-gray-100 dark:bg-gray-700">
    </div>
    <div>
        <label class="block font-semibold">Description</label>
        <textarea name="description" class="w-full rounded px-2 py-1 bg-gray-100 dark:bg-gray-700">{{ product.description if product }}</textarea>
    </div>
    <div>
        <label class="block font-semibold">Price (BDT)</label>
        <input name="price" type="number" step="0.01" value="{{ product.price if product }}" required class="w-full rounded px-2 py-1 bg-gray-100 dark:bg-gray-700">
    </div>
    <div>
        <label class="block font-semibold">Compare At Price (BDT)</label>
        <input name="compare_at_price" type="number" step="0.01" value="{{ product.compare_at_price if product }}" class="w-full rounded px-2 py-1 bg-gray-100 dark:bg-gray-700">
    </div>
    <div>
        <label class="block font-semibold">Images</label>
        {% if product and product.images %}
        <div class="grid grid-cols-4 gap-2">
            {% for img in product.images %}
            <div class="relative">
                <img src="{{ img if img.startswith('http') else '/uploads/' + img }}" class="w-full h-24 object-cover rounded">
                <button hx-post="/admin/products/image/delete/{{ product._id }}/{{ loop.index0 }}" hx-swap="none" class="absolute top-0 right-0 bg-red-500 text-white p-1 rounded-full">X</button>
                <button hx-post="/admin/products/image/move/{{ product._id }}/{{ loop.index0 }}/up" hx-swap="none" class="absolute bottom-0 left-0 bg-blue-500 text-white p-1 rounded">↑</button>
                <button hx-post="/admin/products/image/move/{{ product._id }}/{{ loop.index0 }}/down" hx-swap="none" class="absolute bottom-0 right-0 bg-blue-500 text-white p-1 rounded">↓</button>
            </div>
            {% endfor %}
        </div>
        {% endif %}
        <input type="file" name="images" multiple accept="image/*" class="mt-2">
    </div>
    <div x-data="{ variants: {{ product.variants | tojson if product else '[]' }}, addVariant() { this.variants.push({ color: '', size: '', sku: '', stock: 0, price_override: null }); }, removeVariant(index) { this.variants.splice(index, 1); }, updateSKU(index) { if(this.variants[index].color && this.variants[index].size) { this.variants[index].sku = `${this.variants[index].color}-${this.variants[index].size}`.toUpperCase(); } } }">
        <label class="block font-semibold">Variants</label>
        <div class="space-y-2">
            <template x-for="(variant, index) in variants" :key="index">
                <div class="flex gap-2 items-center">
                    <input x-model="variant.color" placeholder="Color" @input="updateSKU(index)" class="rounded px-2 py-1 bg-gray-100 dark:bg-gray-700">
                    <select x-model="variant.size" @change="updateSKU(index)" class="rounded px-2 py-1 bg-gray-100 dark:bg-gray-700">
                        <option>XS</option>
                        <option>S</option>
                        <option>M</option>
                        <option>L</option>
                        <option>XL</option>
                        <option>XXL</option>
                    </select>
                    <input x-model="variant.stock" type="number" placeholder="Stock" class="rounded px-2 py-1 bg-gray-100 dark:bg-gray-700">
                    <input x-model="variant.price_override" type="number" step="0.01" placeholder="Price Override" class="rounded px-2 py-1 bg-gray-100 dark:bg-gray-700">
                    <input x-model="variant.sku" placeholder="SKU" readonly class="rounded px-2 py-1 bg-gray-100 dark:bg-gray-700">
                    <button type="button" @click="removeVariant(index)" class="bg-red-500 text-white px-2 py-1 rounded-2xl">Remove</button>
                </div>
            </template>
        </div>
        <button type="button" @click="addVariant()" class="mt-2 bg-blue-500 text-white px-4 py-2 rounded-2xl hover:bg-blue-600">Add Variant</button>
        <input type="hidden" name="variants" x-bind:value="JSON.stringify(variants)">
    </div>
    <div>
        <label class="block font-semibold">Status</label>
        <select name="status" class="rounded px-2 py-1 bg-gray-100 dark:bg-gray-700">
            <option value="active" {{ 'selected' if product and product.status == 'active' }}>Active</option>
            <option value="draft" {{ 'selected' if product and product.status == 'draft' }}>Draft</option>
        </select>
    </div>
    <button type="submit" class="bg-green-500 text-white px-4 py-2 rounded-2xl hover:bg-green-600">Save</button>
</form>
{% endblock %}
    ''',

    'admin_orders.html': '''
{% extends "base.html" %}
{% block content %}
<h1 class="text-3xl font-bold">Manage Orders</h1>
<form action="/admin/orders" class="mt-4 flex gap-2 flex-wrap">
    <input name="search" placeholder="Search orders..." value="{{ request.args.get('search') }}" class="rounded px-2 py-1 bg-gray-100 dark:bg-gray-700">
    <select name="status" class="rounded px-2 py-1 bg-gray-100 dark:bg-gray-700">
        <option value="">All Status</option>
        <option value="pending_verification" {{ 'selected' if request.args.get('status') == 'pending_verification' }}>Pending Verification</option>
        <option value="verified" {{ 'selected' if request.args.get('status') == 'verified' }}>Verified</option>
        <option value="processing" {{ 'selected' if request.args.get('status') == 'processing' }}>Processing</option>
        <option value="shipped" {{ 'selected' if request.args.get('status') == 'shipped' }}>Shipped</option>
        <option value="delivered" {{ 'selected' if request.args.get('status') == 'delivered' }}>Delivered</option>
        <option value="canceled" {{ 'selected' if request.args.get('status') == 'canceled' }}>Canceled</option>
        <option value="refunded" {{ 'selected' if request.args.get('status') == 'refunded' }}>Refunded</option>
    </select>
    <button type="submit" class="bg-blue-500 text-white px-4 py-2 rounded-2xl hover:bg-blue-600">Filter</button>
</form>
<table class="w-full mt-4 border-collapse">
    <thead>
        <tr class="bg-gray-100 dark:bg-gray-700">
            <th class="p-2 text-left">Order ID</th>
            <th class="p-2 text-left">Customer</th>
            <th class="p-2 text-left">Total</th>
            <th class="p-2 text-left">Method</th>
            <th class="p-2 text-left">Status</th>
            <th class="p-2 text-left">Date</th>
            <th class="p-2 text-left">Actions</th>
        </tr>
    </thead>
    <tbody>
        {% for order in orders %}
        <tr class="border-b">
            <td class="p-2">{{ order.order_id }}</td>
            <td class="p-2">{{ order.customer.name }} ({{ order.customer.phone }})</td>
            <td class="p-2">{{ format_money(order.amounts.total) }}</td>
            <td class="p-2">{{ order.payment.method | capitalize }}</td>
            <td class="p-2">{{ order.status | capitalize }}</td>
            <td class="p-2">{{ order.created_at | date('YYYY-MM-DD') }}</td>
            <td class="p-2">
                <a href="/admin/orders/{{ order._id }}" class="text-blue-500 hover:underline">View</a>
                <select hx-post="/admin/orders/status/{{ order._id }}" hx-swap="none" hx-confirm="Update status?" class="ml-2 rounded px-2 py-1 bg-gray-100 dark:bg-gray-700">
                    <option value="pending_verification" {{ 'selected' if order.status == 'pending_verification' }}>Pending Verification</option>
                    <option value="verified" {{ 'selected' if order.status == 'verified' }}>Verified</option>
                    <option value="processing" {{ 'selected' if order.status == 'processing' }}>Processing</option>
                    <option value="shipped" {{ 'selected' if order.status == 'shipped' }}>Shipped</option>
                    <option value="delivered" {{ 'selected' if order.status == 'delivered' }}>Delivered</option>
                    <option value="canceled" {{ 'selected' if order.status == 'canceled' }}>Canceled</option>
                    <option value="refunded" {{ 'selected' if order.status == 'refunded' }}>Refunded</option>
                </select>
            </td>
        </tr>
        {% endfor %}
    </tbody>
</table>
<div class="flex justify-center mt-6 gap-2">
    {% if page > 1 %}<a href="?page={{ page - 1 }}&{{ query_string }}" class="px-4 py-2 bg-gray-200 rounded-2xl hover:bg-gray-300">Prev</a>{% endif %}
    {% if page < total_pages %}<a href="?page={{ page + 1 }}&{{ query_string }}" class="px-4 py-2 bg-gray-200 rounded-2xl hover:bg-gray-300">Next</a>{% endif %}
</div>
<a href="/admin/orders/export" class="inline-block mt-4 bg-blue-500 text-white px-4 py-2 rounded-2xl hover:bg-blue-600">Export CSV</a>
{% endblock %}
    ''',

    'admin_order_detail.html': '''
{% extends "base.html" %}
{% block content %}
<h1 class="text-3xl font-bold">Order #{{ order.order_id }}</h1>
<div class="mt-4">
    <h2 class="text-xl font-bold">Items</h2>
    <table class="w-full mt-2">
        {% for item in order.items %}
        <tr>
            <td>{{ item.product_id.name }} ({{ item.variant.color }} / {{ item.variant.size }})</td>
            <td>x{{ item.qty }}</td>
            <td>{{ format_money(item.price * item.qty) }}</td>
        </tr>
        {% endfor %}
    </table>
    <p class="mt-2">Subtotal: {{ format_money(order.amounts.subtotal) }}</p>
    {% if order.amounts.discount > 0 %}<p>Discount: {{ format_money(order.amounts.discount) }}</p>{% endif %}
    <p>Shipping: {{ format_money(order.amounts.shipping) }}</p>
    <p class="text-xl font-bold">Total: {{ format_money(order.amounts.total) }}</p>
</div>
<div class="mt-4">
    <h2 class="text-xl font-bold">Customer</h2>
    <p>{{ order.customer.name }}</p>
    <p>{{ order.customer.phone }}</p>
    <p>{{ order.customer.email }}</p>
</div>
<div class="mt-4">
    <h2 class="text-xl font-bold">Shipping</h2>
    <p>{{ order.shipping.address }}, {{ order.shipping.city }}, {{ order.shipping.postal_code }}</p>
    <p>Method: {{ order.shipping.method }}</p>
    {% if order.shipping.delivery_note %}<p>Note: {{ order.shipping.delivery_note }}</p>{% endif %}
</div>
<div class="mt-4">
    <h2 class="text-xl font-bold">Payment</h2>
    <p>Method: {{ order.payment.method | capitalize }}</p>
    <p>Transaction ID: {{ order.payment.trx_id[-4:] | prepend('****') }}</p>
    {% if order.payment.screenshot_path %}
    <p><a href="/uploads/{{ order.payment.screenshot_path }}" target="_blank">View Screenshot</a></p>
    {% endif %}
    <p>Status: {{ 'Verified' if order.payment.verified else 'Pending' }}</p>
</div>
<div class="mt-4">
    <h2 class="text-xl font-bold">Status</h2>
    <form hx-post="/admin/orders/status/{{ order._id }}" hx-swap="none" class="flex gap-2">
        <select name="status" class="rounded px-2 py-1 bg-gray-100 dark:bg-gray-700">
            <option value="pending_verification" {{ 'selected' if order.status == 'pending_verification' }}>Pending Verification</option>
            <option value="verified" {{ 'selected' if order.status == 'verified' }}>Verified</option>
            <option value="processing" {{ 'selected' if order.status == 'processing' }}>Processing</option>
            <option value="shipped" {{ 'selected' if order.status == 'shipped' }}>Shipped</option>
            <option value="delivered" {{ 'selected' if order.status == 'delivered' }}>Delivered</option>
            <option value="canceled" {{ 'selected' if order.status == 'canceled' }}>Canceled</option>
            <option value="refunded" {{ 'selected' if order.status == 'refunded' }}>Refunded</option>
        </select>
        <button type="submit" class="bg-blue-500 text-white px-4 py-2 rounded-2xl hover:bg-blue-600">Update</button>
    </form>
    {% if order.status in ['canceled', 'refunded'] %}
    <p class="mt-2">Notes: {{ order.notes }}</p>
    {% endif %}
</div>
<form hx-post="/admin/orders/cancel/{{ order._id }}" hx-swap="none" class="mt-4">
    <input type="hidden" name="csrf_token" value="{{ generate_csrf() }}">
    <textarea name="notes" placeholder="Cancel/Refund Notes" class="w-full rounded px-2 py-1 bg-gray-100 dark:bg-gray-700"></textarea>
    <button type="submit" class="bg-red-500 text-white px-4 py-2 rounded-2xl hover:bg-red-600" hx-confirm="Cancel order?">Cancel/Refund</button>
</form>
{% endblock %}
    ''',

    'admin_payments.html': '''
{% extends "base.html" %}
{% block content %}
<h1 class="text-3xl font-bold">Payment Verifications</h1>
<table class="w-full mt-4 border-collapse">
    <thead>
        <tr class="bg-gray-100 dark:bg-gray-700">
            <th class="p-2 text-left">Order ID</th>
            <th class="p-2 text-left">Customer</th>
            <th class="p-2 text-left">Method</th>
            <th class="p-2 text-left">Transaction ID</th>
            <th class="p-2 text-left">Screenshot</th>
            <th class="p-2 text-left">Actions</th>
        </tr>
    </thead>
    <tbody>
        {% for order in orders %}
        <tr class="border-b">
            <td class="p-2">{{ order.order_id }}</td>
            <td class="p-2">{{ order.customer.name }}</td>
            <td class="p-2">{{ order.payment.method | capitalize }}</td>
            <td class="p-2">{{ order.payment.trx_id[-4:] | prepend('****') }}</td>
            <td class="p-2">
                {% if order.payment.screenshot_path %}
                <a href="/uploads/{{ order.payment.screenshot_path }}" target="_blank">
                    <img src="/uploads/{{ make_thumbnail(order.payment.screenshot_path) or order.payment.screenshot_path }}" class="w-16 h-16 object-cover rounded">
                </a>
                {% endif %}
            </td>
            <td class="p-2">
                <button hx-post="/admin/payments/verify/{{ order._id }}" hx-swap="none" class="bg-green-500 text-white px-2 py-1 rounded-2xl hover:bg-green-600">Verify</button>
                <button hx-post="/admin/payments/reject/{{ order._id }}" hx-swap="none" class="bg-red-500 text-white px-2 py-1 rounded-2xl hover:bg-red-600" hx-confirm="Reject payment? Enter reason:" hx-prompt="Reason">Reject</button>
            </td>
        </tr>
        {% endfor %}
    </tbody>
</table>
{% endblock %}
    ''',

    'admin_coupons.html': '''
{% extends "base.html" %}
{% block content %}
<h1 class="text-3xl font-bold">Manage Coupons</h1>
<a href="/admin/coupons/new" class="inline-block mt-4 bg-green-500 text-white px-4 py-2 rounded-2xl hover:bg-green-600">Add New Coupon</a>
<table class="w-full mt-4 border-collapse">
    <thead>
        <tr class="bg-gray-100 dark:bg-gray-700">
            <th class="p-2 text-left">Code</th>
            <th class="p-2 text-left">Type</th>
            <th class="p-2 text-left">Value</th>
            <th class="p-2 text-left">Min Order</th>
            <th class="p-2 text-left">Usage</th>
            <th class="p-2 text-left">Expires</th>
            <th class="p-2 text-left">Status</th>
            <th class="p-2 text-left">Actions</th>
        </tr>
    </thead>
    <tbody>
        {% for coupon in coupons %}
        <tr class="border-b">
            <td class="p-2">{{ coupon.code }}</td>
            <td class="p-2">{{ coupon.type | capitalize }}</td>
            <td class="p-2">{{ coupon.value if coupon.type == 'fixed' else coupon.value ~ '%' }}</td>
            <td class="p-2">{{ format_money(coupon.min_order) }}</td>
            <td class="p-2">{{ coupon.used_count }} / {{ coupon.usage_limit or 'Unlimited' }}</td>
            <td class="p-2">{{ coupon.expires_at | date('YYYY-MM-DD') if coupon.expires_at }}</td>
            <td class="p-2">{{ 'Active' if coupon.active else 'Inactive' }}</td>
            <td class="p-2">
                <a href="/admin/coupons/edit/{{ coupon._id }}" class="text-blue-500 hover:underline">Edit</a>
                <button hx-post="/admin/coupons/toggle/{{ coupon._id }}" hx-swap="none" class="text-blue-500 hover:underline ml-2">{{ 'Deactivate' if coupon.active else 'Activate' }}</button>
                <button hx-post="/admin/coupons/delete/{{ coupon._id }}" hx-swap="none" hx-confirm="Delete coupon?" class="text-red-500 hover:underline ml-2">Delete</button>
            </td>
        </tr>
        {% endfor %}
    </tbody>
</table>
{% endblock %}
    ''',

    'admin_coupon_edit.html': '''
{% extends "base.html" %}
{% block content %}
<h1 class="text-3xl font-bold">{{ 'Edit Coupon' if coupon else 'New Coupon' }}</h1>
<form method="post" class="mt-4 space-y-4">
    <input type="hidden" name="csrf_token" value="{{ generate_csrf() }}">
    <div>
        <label class="block font-semibold">Code</label>
        <input name="code" value="{{ coupon.code if coupon }}" required class="w-full rounded px-2 py-1 bg-gray-100 dark:bg-gray-700">
    </div>
    <div>
        <label class="block font-semibold">Type</label>
        <select name="type" class="w-full rounded px-2 py-1 bg-gray-100 dark:bg-gray-700">
            <option value="percentage" {{ 'selected' if coupon and coupon.type == 'percentage' }}>Percentage</option>
            <option value="fixed" {{ 'selected' if coupon and coupon.type == 'fixed' }}>Fixed Amount</option>
        </select>
    </div>
    <div>
        <label class="block font-semibold">Value</label>
        <input name="value" type="number" step="0.01" value="{{ coupon.value if coupon }}" required class="w-full rounded px-2 py-1 bg-gray-100 dark:bg-gray-700">
    </div>
    <div>
        <label class="block font-semibold">Minimum Order (BDT)</label>
        <input name="min_order" type="number" step="0.01" value="{{ coupon.min_order if coupon }}" required class="w-full rounded px-2 py-1 bg-gray-100 dark:bg-gray-700">
    </div>
    <div>
        <label class="block font-semibold">Usage Limit</label>
        <input name="usage_limit" type="number" value="{{ coupon.usage_limit if coupon }}" class="w-full rounded px-2 py-1 bg-gray-100 dark:bg-gray-700">
    </div>
    <div>
        <label class="block font-semibold">Expires At</label>
        <input name="expires_at" type="date" value="{{ coupon.expires_at | date('YYYY-MM-DD') if coupon.expires_at }}" class="w-full rounded px-2 py-1 bg-gray-100 dark:bg-gray-700">
    </div>
    <div>
        <label class="block font-semibold">Active</label>
        <input type="checkbox" name="active" {{ 'checked' if coupon and coupon.active else '' }} class="rounded">
    </div>
    <button type="submit" class="bg-green-500 text-white px-4 py-2 rounded-2xl hover:bg-green-600">Save</button>
</form>
{% endblock %}
    ''',

    'admin_shipping.html': '''
{% extends "base.html" %}
{% block content %}
<h1 class="text-3xl font-bold">Shipping Settings</h1>
<form method="post" class="mt-4 space-y-4">
    <input type="hidden" name="csrf_token" value="{{ generate_csrf() }}">
    <div>
        <label class="block font-semibold">Free Shipping Threshold (BDT)</label>
        <input name="free_shipping_threshold" type="number" step="0.01" value="{{ settings.free_shipping_threshold }}" class="w-full rounded px-2 py-1 bg-gray-100 dark:bg-gray-700">
    </div>
    <div x-data="{ methods: {{ settings.shipping_methods | tojson }}, addMethod() { this.methods.push({ name: '', fee: 0, desc: '' }); }, removeMethod(index) { this.methods.splice(index, 1); } }">
        <label class="block font-semibold">Shipping Methods</label>
        <div class="space-y-2">
            <template x-for="(method, index) in methods" :key="index">
                <div class="flex gap-2 items-center">
                    <input x-model="method.name" placeholder="Name" class="rounded px-2 py-1 bg-gray-100 dark:bg-gray-700">
                    <input x-model="method.fee" type="number" step="0.01" placeholder="Fee" class="rounded px-2 py-1 bg-gray-100 dark:bg-gray-700">
                    <input x-model="method.desc" placeholder="Description" class="rounded px-2 py-1 bg-gray-100 dark:bg-gray-700">
                    <button type="button" @click="removeMethod(index)" class="bg-red-500 text-white px-2 py-1 rounded-2xl">Remove</button>
                </div>
            </template>
        </div>
        <button type="button" @click="addMethod()" class="mt-2 bg-blue-500 text-white px-4 py-2 rounded-2xl hover:bg-blue-600">Add Method</button>
        <input type="hidden" name="shipping_methods" x-bind:value="JSON.stringify(methods)">
    </div>
    <button type="submit" class="bg-green-500 text-white px-4 py-2 rounded-2xl hover:bg-green-600">Save</button>
</form>
{% endblock %}
    ''',

    'admin_settings.html': '''
{% extends "base.html" %}
{% block content %}
<h1 class="text-3xl font-bold">Settings</h1>
<form method="post" class="mt-4 space-y-4">
    <input type="hidden" name="csrf_token" value="{{ generate_csrf() }}">
    <div>
        <label class="block font-semibold">Brand Name</label>
        <input name="brand" value="{{ settings.brand }}" required class="w-full rounded px-2 py-1 bg-gray-100 dark:bg-gray-700">
    </div>
    <div>
        <label class="block font-semibold">Support Phone</label>
        <input name="support_phone" value="{{ settings.support_phone }}" class="w-full rounded px-2 py-1 bg-gray-100 dark:bg-gray-700">
    </div>
    <div>
        <label class="block font-semibold">Support Email</label>
        <input name="support_email" type="email" value="{{ settings.support_email }}" class="w-full rounded px-2 py-1 bg-gray-100 dark:bg-gray-700">
    </div>
    <div>
        <label class="block font-semibold">bKash Number</label>
        <input name="bkash_number" value="{{ settings.bkash_number }}" class="w-full rounded px-2 py-1 bg-gray-100 dark:bg-gray-700">
    </div>
    <div>
        <label class="block font-semibold">Nagad Number</label>
        <input name="nagad_number" value="{{ settings.nagad_number }}" class="w-full rounded px-2 py-1 bg-gray-100 dark:bg-gray-700">
    </div>
    <div>
        <label class="block font-semibold">Verification SLA (hours)</label>
        <input name="verification_sla" value="{{ settings.verification_sla }}" class="w-full rounded px-2 py-1 bg-gray-100 dark:bg-gray-700">
    </div>
    <div>
        <label class="block font-semibold">SEO Title</label>
        <input name="seo_title" value="{{ settings.seo_title }}" class="w-full rounded px-2 py-1 bg-gray-100 dark:bg-gray-700">
    </div>
    <div>
        <label class="block font-semibold">SEO Description</label>
        <textarea name="seo_desc" class="w-full rounded px-2 py-1 bg-gray-100 dark:bg-gray-700">{{ settings.seo_desc }}</textarea>
    </div>
    <div>
        <label class="block font-semibold">SEO OG Image URL</label>
        <input name="seo_og_image" value="{{ settings.seo_og_image }}" class="w-full rounded px-2 py-1 bg-gray-100 dark:bg-gray-700">
    </div>
    <div>
        <label class="block font-semibold">Maintenance Mode</label>
        <input type="checkbox" name="maintenance" {{ 'checked' if settings.maintenance }} class="rounded">
    </div>
    <button type="submit" class="bg-green-500 text-white px-4 py-2 rounded-2xl hover:bg-green-600">Save</button>
</form>
{% endblock %}
    ''',

    'admin_users.html': '''
{% extends "base.html" %}
{% block content %}
<h1 class="text-3xl font-bold">Users</h1>
<table class="w-full mt-4 border-collapse">
    <thead>
        <tr class="bg-gray-100 dark:bg-gray-700">
            <th class="p-2 text-left">Name</th>
            <th class="p-2 text-left">Phone</th>
            <th class="p-2 text-left">Email</th>
            <th class="p-2 text-left">Orders</th>
        </tr>
    </thead>
    <tbody>
        {% for user in users %}
        <tr class="border-b">
            <td class="p-2">{{ user.name }}</td>
            <td class="p-2">{{ user.phone }}</td>
            <td class="p-2">{{ user.email }}</td>
            <td class="p-2">{{ user.order_count }}</td>
        </tr>
        {% endfor %}
    </tbody>
</table>
<a href="/admin/users/export" class="inline-block mt-4 bg-blue-500 text-white px-4 py-2 rounded-2xl hover:bg-blue-600">Export CSV</a>
{% endblock %}
    '''
}

# Jinja environment
app.jinja_env = Environment(loader=DictLoader(templates))

# Custom filters
app.jinja_env.filters['date'] = lambda dt, fmt: dt.strftime(fmt) if dt else ''
app.jinja_env.filters['tojson'] = json.dumps
app.jinja_env.filters['prepend']
