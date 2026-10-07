from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, send_file, make_response
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, timedelta, date
from functools import wraps
from io import BytesIO
import os
import calendar as cal_mod

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, HRFlowable
from reportlab.lib.enums import TA_RIGHT, TA_CENTER, TA_LEFT

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev-secret-key-change-in-production')
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///rental_inventory.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'
login_manager.login_message_category = 'info'

# ==================== MODELS ====================

class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    role = db.Column(db.String(20), default='user')  # admin | user
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)


class Category(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), unique=True, nullable=False)
    description = db.Column(db.String(255))
    items = db.relationship('Item', backref='category', lazy=True)


class EventType(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), unique=True, nullable=False)
    description = db.Column(db.String(255))
    multiplier = db.Column(db.Float, default=1.0)


class Customer(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=False)
    email = db.Column(db.String(150))
    phone = db.Column(db.String(50))
    company = db.Column(db.String(150))
    notes = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    quotes = db.relationship('Quote', backref='customer', lazy=True)
    invoices = db.relationship('Invoice', backref='customer', lazy=True)


class Item(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=False)
    description = db.Column(db.Text)
    category_id = db.Column(db.Integer, db.ForeignKey('category.id'), nullable=False)
    sku = db.Column(db.String(50), unique=True)
    quantity_total = db.Column(db.Integer, default=0)
    base_daily_rate = db.Column(db.Float, nullable=False)
    weekend_multiplier = db.Column(db.Float, default=1.25)  # premium when rental includes Sat/Sun
    replacement_cost = db.Column(db.Float, default=0.0)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    price_overrides = db.relationship('ItemPriceOverride', backref='item', lazy=True, cascade='all, delete-orphan')
    bookings = db.relationship('Booking', backref='item', lazy=True)


class ItemPriceOverride(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    item_id = db.Column(db.Integer, db.ForeignKey('item.id'), nullable=False)
    event_type_id = db.Column(db.Integer, db.ForeignKey('event_type.id'), nullable=False)
    daily_rate = db.Column(db.Float, nullable=False)
    event_type = db.relationship('EventType')


class RateTier(db.Model):
    """Multi-day discount tiers. min_days inclusive. discount_percent off the daily rate."""
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), nullable=False)
    min_days = db.Column(db.Integer, nullable=False)
    discount_percent = db.Column(db.Float, default=0.0)  # e.g. 15 = 15% off


class Quote(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    quote_number = db.Column(db.String(30), unique=True, nullable=False)
    customer_id = db.Column(db.Integer, db.ForeignKey('customer.id'))
    customer_name = db.Column(db.String(150), nullable=False)  # denormalized for quick display
    customer_email = db.Column(db.String(150))
    customer_phone = db.Column(db.String(50))
    event_type_id = db.Column(db.Integer, db.ForeignKey('event_type.id'), nullable=False)
    event_date = db.Column(db.Date)
    event_location = db.Column(db.String(255))
    rental_start = db.Column(db.Date)
    rental_end = db.Column(db.Date)
    notes = db.Column(db.Text)
    status = db.Column(db.String(30), default='draft')  # draft, sent, accepted, declined, expired
    apply_weekend_premium = db.Column(db.Boolean, default=False)
    multi_day_discount_percent = db.Column(db.Float, default=0.0)  # from rate tier
    subtotal = db.Column(db.Float, default=0.0)
    discount_percent = db.Column(db.Float, default=0.0)  # manual extra discount
    tax_percent = db.Column(db.Float, default=0.0)
    total = db.Column(db.Float, default=0.0)
    created_by = db.Column(db.Integer, db.ForeignKey('user.id'))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    event_type = db.relationship('EventType')
    creator = db.relationship('User')
    line_items = db.relationship('QuoteLineItem', backref='quote', lazy=True, cascade='all, delete-orphan')
    invoice = db.relationship('Invoice', backref='quote', uselist=False)
    bookings = db.relationship('Booking', backref='quote', lazy=True)


class QuoteLineItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    quote_id = db.Column(db.Integer, db.ForeignKey('quote.id'), nullable=False)
    item_id = db.Column(db.Integer, db.ForeignKey('item.id'), nullable=False)
    quantity = db.Column(db.Integer, nullable=False, default=1)
    days = db.Column(db.Integer, default=1)
    unit_price = db.Column(db.Float, nullable=False)  # final unit after event/weekend/tier
    line_total = db.Column(db.Float, nullable=False)
    item = db.relationship('Item')


class Booking(db.Model):
    """Locks inventory for an accepted quote over a date range."""
    id = db.Column(db.Integer, primary_key=True)
    quote_id = db.Column(db.Integer, db.ForeignKey('quote.id'), nullable=False)
    item_id = db.Column(db.Integer, db.ForeignKey('item.id'), nullable=False)
    quantity = db.Column(db.Integer, nullable=False)
    start_date = db.Column(db.Date, nullable=False)
    end_date = db.Column(db.Date, nullable=False)
    status = db.Column(db.String(20), default='active')  # active, cancelled, completed
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class Invoice(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    invoice_number = db.Column(db.String(30), unique=True, nullable=False)
    quote_id = db.Column(db.Integer, db.ForeignKey('quote.id'), nullable=False)
    customer_id = db.Column(db.Integer, db.ForeignKey('customer.id'))
    customer_name = db.Column(db.String(150), nullable=False)
    customer_email = db.Column(db.String(150))
    issue_date = db.Column(db.Date, default=date.today)
    due_date = db.Column(db.Date)
    subtotal = db.Column(db.Float, default=0.0)
    tax_percent = db.Column(db.Float, default=0.0)
    total = db.Column(db.Float, default=0.0)
    amount_paid = db.Column(db.Float, default=0.0)
    status = db.Column(db.String(30), default='unpaid')  # unpaid, partial, paid, void
    notes = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    line_items = db.relationship('InvoiceLineItem', backref='invoice', lazy=True, cascade='all, delete-orphan')


class InvoiceLineItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    invoice_id = db.Column(db.Integer, db.ForeignKey('invoice.id'), nullable=False)
    description = db.Column(db.String(255), nullable=False)
    quantity = db.Column(db.Integer, default=1)
    unit_price = db.Column(db.Float, nullable=False)
    line_total = db.Column(db.Float, nullable=False)



class Review(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    author_name = db.Column(db.String(120), nullable=False)
    author_role = db.Column(db.String(120))  # e.g. Wedding client, Venue
    rating = db.Column(db.Integer, default=5)  # 1-5
    text = db.Column(db.Text, nullable=False)
    approved = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


# ==================== HELPERS ====================

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_authenticated or current_user.role != 'admin':
            flash('Admin access required.', 'danger')
            return redirect(url_for('dashboard'))
        return f(*args, **kwargs)
    return decorated


def get_item_price(item, event_type, days=1, apply_weekend=False, rental_start=None, rental_end=None):
    """Base price after event multiplier / override, then weekend premium, then multi-day tier."""
    # 1. Event-type price
    unit = item.base_daily_rate
    for ov in item.price_overrides:
        if ov.event_type_id == event_type.id:
            unit = ov.daily_rate
            break
    else:
        unit = round(item.base_daily_rate * event_type.multiplier, 2)

    # 2. Weekend premium (if rental spans a weekend day)
    if apply_weekend and rental_start and rental_end:
        d = rental_start
        while d <= rental_end:
            if d.weekday() >= 5:  # Sat=5, Sun=6
                unit = round(unit * item.weekend_multiplier, 2)
                break
            d += timedelta(days=1)
    elif apply_weekend:
        unit = round(unit * item.weekend_multiplier, 2)

    # 3. Multi-day tier discount
    tier_discount = get_tier_discount(days)
    if tier_discount > 0:
        unit = round(unit * (1 - tier_discount / 100), 2)

    return unit


def get_tier_discount(days):
    tiers = RateTier.query.order_by(RateTier.min_days.desc()).all()
    for t in tiers:
        if days >= t.min_days:
            return t.discount_percent
    return 0.0


def available_quantity(item, start_date, end_date, exclude_quote_id=None):
    """How many units are free for the given date range (considering active bookings)."""
    if not start_date or not end_date:
        return item.quantity_total
    q = Booking.query.filter(
        Booking.item_id == item.id,
        Booking.status == 'active',
        Booking.start_date <= end_date,
        Booking.end_date >= start_date,
    )
    if exclude_quote_id:
        q = q.filter(Booking.quote_id != exclude_quote_id)
    reserved = sum(b.quantity for b in q.all())
    return max(0, item.quantity_total - reserved)


def generate_quote_number():
    today = datetime.utcnow().strftime('%Y%m%d')
    last = Quote.query.filter(Quote.quote_number.like(f'Q-{today}%')).order_by(Quote.id.desc()).first()
    seq = int(last.quote_number.split('-')[-1]) + 1 if last else 1
    return f'Q-{today}-{seq:04d}'


def generate_invoice_number():
    today = datetime.utcnow().strftime('%Y%m%d')
    last = Invoice.query.filter(Invoice.invoice_number.like(f'INV-{today}%')).order_by(Invoice.id.desc()).first()
    seq = int(last.invoice_number.split('-')[-1]) + 1 if last else 1
    return f'INV-{today}-{seq:04d}'


def create_bookings_for_quote(quote):
    """Lock inventory when a quote is accepted."""
    if not quote.rental_start or not quote.rental_end:
        return False, 'Quote needs rental start and end dates to create bookings.'
    # Clear any previous bookings for this quote
    Booking.query.filter_by(quote_id=quote.id).delete()
    for li in quote.line_items:
        avail = available_quantity(li.item, quote.rental_start, quote.rental_end, exclude_quote_id=quote.id)
        if li.quantity > avail:
            db.session.rollback()
            return False, f'Not enough stock for {li.item.name}: need {li.quantity}, available {avail}.'
        b = Booking(
            quote_id=quote.id,
            item_id=li.item_id,
            quantity=li.quantity,
            start_date=quote.rental_start,
            end_date=quote.rental_end,
            status='active',
        )
        db.session.add(b)
    return True, None


def cancel_bookings_for_quote(quote):
    Booking.query.filter_by(quote_id=quote.id).update({'status': 'cancelled'})


def build_pdf_quote(quote):
    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter, rightMargin=0.6*inch, leftMargin=0.6*inch,
                            topMargin=0.6*inch, bottomMargin=0.6*inch)
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name='Right', alignment=TA_RIGHT))
    styles.add(ParagraphStyle(name='Center', alignment=TA_CENTER, fontSize=16, spaceAfter=6))
    styles.add(ParagraphStyle(name='Small', fontSize=9, textColor=colors.grey))
    story = []

    # Logo
    logo_path = os.path.join(app.root_path, 'static', 'img', 'logo.jpg')
    if os.path.exists(logo_path):
        from reportlab.platypus import Image as RLImage
        logo = RLImage(logo_path, width=1.4*inch, height=1.4*inch)
        logo.hAlign = 'CENTER'
        story.append(logo)
        story.append(Spacer(1, 4))
    story.append(Paragraph('PB&amp;J LIVE SOUND', styles['Center']))
    story.append(Paragraph('Live Sound · Lighting · Production', styles['Small']))
    story.append(Paragraph(f'<b>QUOTE</b>  {quote.quote_number}', styles['Center']))
    story.append(Spacer(1, 6))
    story.append(HRFlowable(width='100%', thickness=1.5, color=colors.HexColor('#ff6b00')))
    story.append(Spacer(1, 12))

    left = f"""
    <b>Bill To</b><br/>
    {quote.customer_name}<br/>
    {quote.customer_email or ''}<br/>
    {quote.customer_phone or ''}
    """
    right = f"""
    <b>Event</b>: {quote.event_type.name}<br/>
    <b>Event Date</b>: {quote.event_date or '—'}<br/>
    <b>Location</b>: {quote.event_location or '—'}<br/>
    <b>Rental</b>: {quote.rental_start or '—'} → {quote.rental_end or '—'}
    """
    info = Table([[Paragraph(left, styles['Normal']), Paragraph(right, styles['Right'])]],
                 colWidths=[3.5*inch, 3.5*inch])
    info.setStyle(TableStyle([('VALIGN', (0, 0), (-1, -1), 'TOP')]))
    story.append(info)
    story.append(Spacer(1, 16))

    data = [['Item', 'Qty', 'Days', 'Unit $/day', 'Line Total']]
    for li in quote.line_items:
        data.append([
            li.item.name,
            str(li.quantity),
            str(li.days),
            f'${li.unit_price:.2f}',
            f'${li.line_total:.2f}',
        ])
    data.append(['', '', '', 'Subtotal', f'${quote.subtotal:.2f}'])
    if quote.multi_day_discount_percent:
        data.append(['', '', '', f'Multi-day disc. ({quote.multi_day_discount_percent}%)', 'applied in unit'])
    if quote.discount_percent:
        disc = quote.subtotal * quote.discount_percent / 100
        data.append(['', '', '', f'Discount ({quote.discount_percent}%)', f'-${disc:.2f}'])
    if quote.tax_percent:
        taxable = quote.subtotal * (1 - quote.discount_percent / 100)
        tax = taxable * quote.tax_percent / 100
        data.append(['', '', '', f'Tax ({quote.tax_percent}%)', f'${tax:.2f}'])
    data.append(['', '', '', 'TOTAL', f'${quote.total:.2f}'])

    t = Table(data, colWidths=[3.2*inch, 0.7*inch, 0.7*inch, 1.2*inch, 1.2*inch])
    t.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#1a1a2e')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, -1), 9),
        ('ALIGN', (1, 0), (-1, -1), 'RIGHT'),
        ('GRID', (0, 0), (-1, -len(data)+1), 0.4, colors.HexColor('#e2e8f0')),
        ('FONTNAME', (0, -1), (-1, -1), 'Helvetica-Bold'),
        ('BACKGROUND', (0, -1), (-1, -1), colors.HexColor('#f1f5f9')),
        ('TOPPADDING', (0, 0), (-1, -1), 5),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
    ]))
    story.append(t)

    if quote.notes:
        story.append(Spacer(1, 14))
        story.append(Paragraph(f'<b>Notes</b>: {quote.notes}', styles['Normal']))

    story.append(Spacer(1, 20))
    story.append(Paragraph(f'Status: {quote.status.upper()}  |  Generated {datetime.utcnow().strftime("%Y-%m-%d %H:%M")} UTC', styles['Small']))
    story.append(Paragraph('PB&amp;J Live Sound · Lighting · Production', styles['Small']))
    doc.build(story)
    buffer.seek(0)
    return buffer


def build_pdf_invoice(invoice):
    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter, rightMargin=0.6*inch, leftMargin=0.6*inch,
                            topMargin=0.6*inch, bottomMargin=0.6*inch)
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name='Right', alignment=TA_RIGHT))
    styles.add(ParagraphStyle(name='Center', alignment=TA_CENTER, fontSize=16, spaceAfter=6))
    styles.add(ParagraphStyle(name='Small', fontSize=9, textColor=colors.grey))
    story = []

    logo_path = os.path.join(app.root_path, 'static', 'img', 'logo.jpg')
    if os.path.exists(logo_path):
        from reportlab.platypus import Image as RLImage
        logo = RLImage(logo_path, width=1.4*inch, height=1.4*inch)
        logo.hAlign = 'CENTER'
        story.append(logo)
        story.append(Spacer(1, 4))
    story.append(Paragraph('PB&amp;J LIVE SOUND', styles['Center']))
    story.append(Paragraph('Live Sound · Lighting · Production', styles['Small']))
    story.append(Paragraph(f'<b>INVOICE</b>  {invoice.invoice_number}', styles['Center']))
    story.append(Spacer(1, 6))
    story.append(HRFlowable(width='100%', thickness=1.5, color=colors.HexColor('#ff6b00')))
    story.append(Spacer(1, 12))

    left = f"""
    <b>Bill To</b><br/>
    {invoice.customer_name}<br/>
    {invoice.customer_email or ''}
    """
    right = f"""
    <b>Issue Date</b>: {invoice.issue_date}<br/>
    <b>Due Date</b>: {invoice.due_date or '—'}<br/>
    <b>Status</b>: {invoice.status.upper()}<br/>
    <b>Quote</b>: {invoice.quote.quote_number if invoice.quote else '—'}
    """
    info = Table([[Paragraph(left, styles['Normal']), Paragraph(right, styles['Right'])]],
                 colWidths=[3.5*inch, 3.5*inch])
    info.setStyle(TableStyle([('VALIGN', (0, 0), (-1, -1), 'TOP')]))
    story.append(info)
    story.append(Spacer(1, 16))

    data = [['Description', 'Qty', 'Unit Price', 'Line Total']]
    for li in invoice.line_items:
        data.append([li.description, str(li.quantity), f'${li.unit_price:.2f}', f'${li.line_total:.2f}'])
    data.append(['', '', 'Subtotal', f'${invoice.subtotal:.2f}'])
    if invoice.tax_percent:
        tax = invoice.subtotal * invoice.tax_percent / 100
        data.append(['', '', f'Tax ({invoice.tax_percent}%)', f'${tax:.2f}'])
    data.append(['', '', 'TOTAL', f'${invoice.total:.2f}'])
    data.append(['', '', 'Amount Paid', f'${invoice.amount_paid:.2f}'])
    balance = invoice.total - invoice.amount_paid
    data.append(['', '', 'BALANCE DUE', f'${balance:.2f}'])

    t = Table(data, colWidths=[3.8*inch, 0.8*inch, 1.2*inch, 1.2*inch])
    t.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#1a1a2e')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, -1), 9),
        ('ALIGN', (1, 0), (-1, -1), 'RIGHT'),
        ('GRID', (0, 0), (-1, -len(data)+3), 0.4, colors.HexColor('#e2e8f0')),
        ('FONTNAME', (0, -1), (-1, -1), 'Helvetica-Bold'),
        ('BACKGROUND', (0, -1), (-1, -1), colors.HexColor('#fef3c7')),
        ('TOPPADDING', (0, 0), (-1, -1), 5),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
    ]))
    story.append(t)

    if invoice.notes:
        story.append(Spacer(1, 14))
        story.append(Paragraph(f'<b>Notes</b>: {invoice.notes}', styles['Normal']))

    story.append(Spacer(1, 20))
    story.append(Paragraph(f'Generated {datetime.utcnow().strftime("%Y-%m-%d %H:%M")} UTC  |  PB&amp;J Live Sound', styles['Small']))
    doc.build(story)
    buffer.seek(0)
    return buffer


# ==================== AUTH ====================

@app.route('/register', methods=['GET', 'POST'])
def register():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        email = request.form.get('email', '').strip()
        password = request.form.get('password', '')
        if not username or not email or not password:
            flash('All fields are required.', 'danger')
            return render_template('register.html')
        if User.query.filter_by(username=username).first():
            flash('Username already taken.', 'danger')
            return render_template('register.html')
        if User.query.filter_by(email=email).first():
            flash('Email already registered.', 'danger')
            return render_template('register.html')
        user = User(username=username, email=email, role='user')
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        flash('Registration successful. Please log in.', 'success')
        return redirect(url_for('login'))
    return render_template('register.html')


@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        user = User.query.filter_by(username=username).first()
        if user and user.check_password(password):
            login_user(user, remember=True)
            return redirect(request.args.get('next') or url_for('dashboard'))
        flash('Invalid username or password.', 'danger')
    return render_template('login.html')


@app.route('/logout')
@login_required
def logout():
    logout_user()
    flash('You have been logged out.', 'info')
    return redirect(url_for('login'))


# ==================== DASHBOARD ====================

@app.route('/')
def home():
    return render_template('public/home.html')


@app.route('/about')
def about():
    return render_template('public/about.html')


@app.route('/services')
def services():
    return render_template('public/services.html')



@app.route('/testimonials', methods=['GET', 'POST'])
def testimonials():
    if request.method == 'POST':
        name = request.form.get('author_name', '').strip()
        role = request.form.get('author_role', '').strip()
        rating = request.form.get('rating', '5')
        body = request.form.get('text', '').strip()
        try:
            rating = max(1, min(5, int(rating)))
        except ValueError:
            rating = 5
        if name and body and len(body) >= 10:
            rev = Review(author_name=name, author_role=role or None, rating=rating, text=body, approved=False)
            db.session.add(rev)
            db.session.commit()
            flash('Thank you! Your review was submitted and will appear after we approve it.', 'success')
            return redirect(url_for('testimonials'))
        flash('Please enter your name and a review (at least a few sentences).', 'danger')
    reviews = Review.query.filter_by(approved=True).order_by(Review.created_at.desc()).all()
    return render_template('public/testimonials.html', reviews=reviews)


@app.route('/gallery')
def gallery():
    photos = [
        {'file': '01_stage_red.jpg', 'caption': 'Full stage production — red wash & beam effects'},
        {'file': '02_stage_blue.jpg', 'caption': 'Club stage with truss lighting & PA'},
        {'file': '07_truss_rig.jpg', 'caption': 'Moving-head rig on truss'},
        {'file': '08_foh_console.jpg', 'caption': 'Front-of-house mix'},
        {'file': '10_hall_setup.jpg', 'caption': 'Hall setup — line array & stage package'},
        {'file': '05_crowd_blue.jpg', 'caption': 'Live show — blue production look'},
        {'file': '06_crowd_red.jpg', 'caption': 'Packed room, full lighting design'},
        {'file': '04_live_green.jpg', 'caption': 'Band under green beam wash'},
        {'file': '12_stage_energy.jpg', 'caption': 'High-energy stage with full PA'},
        {'file': '09_blues_stage.jpg', 'caption': 'Community showcase — stage & lighting'},
        {'file': '11_blues_band.jpg', 'caption': 'Live band — colorful stage wash'},
        {'file': '17_red_wash.jpg', 'caption': 'Intimate room, dramatic red lighting'},
        {'file': '03_outdoor_band.jpg', 'caption': 'Outdoor barn stage'},
        {'file': '13_outdoor_day.jpg', 'caption': 'Waterfront stage — daytime festival'},
        {'file': '14_waterfront.jpg', 'caption': 'Waterfront production with full band'},
        {'file': '15_crowd_phones.jpg', 'caption': 'Crowd moment under blue truss'},
        {'file': '16_ballroom.jpg', 'caption': 'Ballroom event production'},
    ]
    return render_template('public/gallery.html', photos=photos)


@app.route('/contact', methods=['GET', 'POST'])
def contact():
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        email = request.form.get('email', '').strip()
        phone = request.form.get('phone', '').strip()
        message = request.form.get('message', '').strip()
        event_date = request.form.get('event_date', '')
        if name and (email or phone) and message:
            flash('Thanks! We received your message and will get back to you soon.', 'success')
            return redirect(url_for('contact'))
        flash('Please fill in your name, contact info, and message.', 'danger')
    return render_template('public/contact.html')


@app.route('/dashboard')
@login_required
def dashboard():
    total_items = Item.query.filter_by(is_active=True).count()
    open_quotes = Quote.query.filter(Quote.status.in_(['draft', 'sent'])).count()
    active_bookings = Booking.query.filter_by(status='active').count()
    unpaid_invoices = Invoice.query.filter(Invoice.status.in_(['unpaid', 'partial'])).count()
    recent_quotes = Quote.query.order_by(Quote.created_at.desc()).limit(5).all()
    upcoming = Booking.query.filter(
        Booking.status == 'active',
        Booking.start_date >= date.today()
    ).order_by(Booking.start_date).limit(8).all()
    return render_template('dashboard.html',
                           total_items=total_items,
                           open_quotes=open_quotes,
                           active_bookings=active_bookings,
                           unpaid_invoices=unpaid_invoices,
                           recent_quotes=recent_quotes,
                           upcoming=upcoming)


# ==================== CUSTOMERS ====================

@app.route('/customers')
@login_required
def customers():
    q = request.args.get('q', '').strip()
    query = Customer.query
    if q:
        query = query.filter(
            db.or_(Customer.name.ilike(f'%{q}%'),
                   Customer.email.ilike(f'%{q}%'),
                   Customer.company.ilike(f'%{q}%'))
        )
    custs = query.order_by(Customer.name).all()
    return render_template('customers.html', customers=custs, search=q)


@app.route('/customers/add', methods=['GET', 'POST'])
@login_required
def add_customer():
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        if not name:
            flash('Name is required.', 'danger')
            return render_template('customer_form.html', customer=None)
        c = Customer(
            name=name,
            email=request.form.get('email', '').strip(),
            phone=request.form.get('phone', '').strip(),
            company=request.form.get('company', '').strip(),
            notes=request.form.get('notes', ''),
        )
        db.session.add(c)
        db.session.commit()
        flash(f'Customer "{name}" added.', 'success')
        return redirect(url_for('view_customer', customer_id=c.id))
    return render_template('customer_form.html', customer=None)


@app.route('/customers/<int:customer_id>')
@login_required
def view_customer(customer_id):
    c = Customer.query.get_or_404(customer_id)
    return render_template('customer_view.html', customer=c)


@app.route('/customers/<int:customer_id>/edit', methods=['GET', 'POST'])
@login_required
def edit_customer(customer_id):
    c = Customer.query.get_or_404(customer_id)
    if request.method == 'POST':
        c.name = request.form.get('name', '').strip()
        c.email = request.form.get('email', '').strip()
        c.phone = request.form.get('phone', '').strip()
        c.company = request.form.get('company', '').strip()
        c.notes = request.form.get('notes', '')
        db.session.commit()
        flash('Customer updated.', 'success')
        return redirect(url_for('view_customer', customer_id=c.id))
    return render_template('customer_form.html', customer=c)


# ==================== INVENTORY ====================

@app.route('/inventory')
@login_required
def inventory():
    category_id = request.args.get('category')
    q = request.args.get('q', '').strip()
    query = Item.query.filter_by(is_active=True)
    if category_id:
        query = query.filter_by(category_id=category_id)
    if q:
        query = query.filter(db.or_(Item.name.ilike(f'%{q}%'), Item.sku.ilike(f'%{q}%')))
    items = query.order_by(Item.name).all()
    categories = Category.query.order_by(Category.name).all()
    return render_template('inventory.html', items=items, categories=categories,
                           selected_category=category_id, search=q)


@app.route('/inventory/add', methods=['GET', 'POST'])
@login_required
@admin_required
def add_item():
    categories = Category.query.order_by(Category.name).all()
    event_types = EventType.query.order_by(EventType.name).all()
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        category_id = request.form.get('category_id')
        base_rate = request.form.get('base_daily_rate')
        qty = request.form.get('quantity_total', 0)
        if not name or not category_id or not base_rate:
            flash('Name, category and base rate are required.', 'danger')
            return render_template('item_form.html', categories=categories, event_types=event_types, item=None)
        item = Item(
            name=name,
            description=request.form.get('description', ''),
            category_id=int(category_id),
            sku=request.form.get('sku') or None,
            quantity_total=int(qty or 0),
            base_daily_rate=float(base_rate),
            weekend_multiplier=float(request.form.get('weekend_multiplier') or 1.25),
            replacement_cost=float(request.form.get('replacement_cost') or 0),
        )
        db.session.add(item)
        db.session.flush()
        for et in event_types:
            override_val = request.form.get(f'override_{et.id}')
            if override_val:
                try:
                    rate = float(override_val)
                    if rate > 0:
                        db.session.add(ItemPriceOverride(item_id=item.id, event_type_id=et.id, daily_rate=rate))
                except ValueError:
                    pass
        db.session.commit()
        flash(f'Item "{name}" added.', 'success')
        return redirect(url_for('inventory'))
    return render_template('item_form.html', categories=categories, event_types=event_types, item=None)


@app.route('/inventory/<int:item_id>/edit', methods=['GET', 'POST'])
@login_required
@admin_required
def edit_item(item_id):
    item = Item.query.get_or_404(item_id)
    categories = Category.query.order_by(Category.name).all()
    event_types = EventType.query.order_by(EventType.name).all()
    if request.method == 'POST':
        item.name = request.form.get('name', '').strip()
        item.description = request.form.get('description', '')
        item.category_id = int(request.form.get('category_id'))
        item.sku = request.form.get('sku') or None
        item.quantity_total = int(request.form.get('quantity_total') or 0)
        item.base_daily_rate = float(request.form.get('base_daily_rate'))
        item.weekend_multiplier = float(request.form.get('weekend_multiplier') or 1.25)
        item.replacement_cost = float(request.form.get('replacement_cost') or 0)
        ItemPriceOverride.query.filter_by(item_id=item.id).delete()
        for et in event_types:
            override_val = request.form.get(f'override_{et.id}')
            if override_val:
                try:
                    rate = float(override_val)
                    if rate > 0:
                        db.session.add(ItemPriceOverride(item_id=item.id, event_type_id=et.id, daily_rate=rate))
                except ValueError:
                    pass
        db.session.commit()
        flash('Item updated.', 'success')
        return redirect(url_for('inventory'))
    overrides = {o.event_type_id: o.daily_rate for o in item.price_overrides}
    return render_template('item_form.html', categories=categories, event_types=event_types,
                           item=item, overrides=overrides)


@app.route('/inventory/<int:item_id>/delete', methods=['POST'])
@login_required
@admin_required
def delete_item(item_id):
    item = Item.query.get_or_404(item_id)
    item.is_active = False
    db.session.commit()
    flash(f'Item "{item.name}" deactivated.', 'info')
    return redirect(url_for('inventory'))


# ==================== SETTINGS ====================

@app.route('/settings')
@login_required
@admin_required
def settings():
    categories = Category.query.order_by(Category.name).all()
    event_types = EventType.query.order_by(EventType.name).all()
    tiers = RateTier.query.order_by(RateTier.min_days).all()
    return render_template('settings.html', categories=categories, event_types=event_types, tiers=tiers)


@app.route('/settings/category/add', methods=['POST'])
@login_required
@admin_required
def add_category():
    name = request.form.get('name', '').strip()
    if name and not Category.query.filter_by(name=name).first():
        db.session.add(Category(name=name, description=request.form.get('description', '')))
        db.session.commit()
        flash('Category added.', 'success')
    return redirect(url_for('settings'))


@app.route('/settings/eventtype/add', methods=['POST'])
@login_required
@admin_required
def add_event_type():
    name = request.form.get('name', '').strip()
    mult = float(request.form.get('multiplier') or 1.0)
    if name and not EventType.query.filter_by(name=name).first():
        db.session.add(EventType(name=name, description=request.form.get('description', ''), multiplier=mult))
        db.session.commit()
        flash('Event type added.', 'success')
    return redirect(url_for('settings'))


@app.route('/settings/eventtype/<int:et_id>/edit', methods=['POST'])
@login_required
@admin_required
def edit_event_type(et_id):
    et = EventType.query.get_or_404(et_id)
    et.name = request.form.get('name', et.name).strip()
    et.multiplier = float(request.form.get('multiplier') or et.multiplier)
    et.description = request.form.get('description', '')
    db.session.commit()
    flash('Event type updated.', 'success')
    return redirect(url_for('settings'))


@app.route('/settings/tier/add', methods=['POST'])
@login_required
@admin_required
def add_tier():
    name = request.form.get('name', '').strip()
    min_days = int(request.form.get('min_days') or 1)
    disc = float(request.form.get('discount_percent') or 0)
    if name:
        db.session.add(RateTier(name=name, min_days=min_days, discount_percent=disc))
        db.session.commit()
        flash('Rate tier added.', 'success')
    return redirect(url_for('settings'))


@app.route('/settings/tier/<int:tier_id>/delete', methods=['POST'])
@login_required
@admin_required
def delete_tier(tier_id):
    t = RateTier.query.get_or_404(tier_id)
    db.session.delete(t)
    db.session.commit()
    flash('Rate tier removed.', 'info')
    return redirect(url_for('settings'))


# ==================== QUOTES ====================

@app.route('/quotes')
@login_required
def quotes():
    status = request.args.get('status')
    query = Quote.query
    if status:
        query = query.filter_by(status=status)
    quotes_list = query.order_by(Quote.created_at.desc()).all()
    return render_template('quotes.html', quotes=quotes_list, selected_status=status)


@app.route('/quotes/new', methods=['GET', 'POST'])
@login_required
def new_quote():
    event_types = EventType.query.order_by(EventType.name).all()
    items = Item.query.filter_by(is_active=True).order_by(Item.name).all()
    customers = Customer.query.order_by(Customer.name).all()
    tiers = RateTier.query.order_by(RateTier.min_days).all()
    if request.method == 'POST':
        event_type_id = int(request.form.get('event_type_id'))
        event_type = EventType.query.get(event_type_id)
        customer_id = request.form.get('customer_id') or None
        if customer_id:
            customer_id = int(customer_id)
        rental_start = datetime.strptime(request.form.get('rental_start'), '%Y-%m-%d').date() if request.form.get('rental_start') else None
        rental_end = datetime.strptime(request.form.get('rental_end'), '%Y-%m-%d').date() if request.form.get('rental_end') else None
        apply_weekend = bool(request.form.get('apply_weekend_premium'))
        days_default = 1
        if rental_start and rental_end:
            days_default = max(1, (rental_end - rental_start).days + 1)

        quote = Quote(
            quote_number=generate_quote_number(),
            customer_id=customer_id,
            customer_name=request.form.get('customer_name', '').strip(),
            customer_email=request.form.get('customer_email', '').strip(),
            customer_phone=request.form.get('customer_phone', '').strip(),
            event_type_id=event_type_id,
            event_date=datetime.strptime(request.form.get('event_date'), '%Y-%m-%d').date() if request.form.get('event_date') else None,
            event_location=request.form.get('event_location', ''),
            rental_start=rental_start,
            rental_end=rental_end,
            notes=request.form.get('notes', ''),
            status='draft',
            apply_weekend_premium=apply_weekend,
            discount_percent=float(request.form.get('discount_percent') or 0),
            tax_percent=float(request.form.get('tax_percent') or 0),
            created_by=current_user.id,
        )
        db.session.add(quote)
        db.session.flush()

        item_ids = request.form.getlist('item_id[]')
        quantities = request.form.getlist('quantity[]')
        days_list = request.form.getlist('days[]')
        subtotal = 0.0
        max_days = 1
        for iid, qty, days in zip(item_ids, quantities, days_list):
            if not iid or not qty:
                continue
            item = Item.query.get(int(iid))
            if not item:
                continue
            q = int(qty)
            d = int(days or days_default)
            max_days = max(max_days, d)
            unit = get_item_price(item, event_type, days=d, apply_weekend=apply_weekend,
                                  rental_start=rental_start, rental_end=rental_end)
            line_total = round(unit * q * d, 2)
            subtotal += line_total
            db.session.add(QuoteLineItem(
                quote_id=quote.id, item_id=item.id, quantity=q, days=d,
                unit_price=unit, line_total=line_total
            ))
        quote.multi_day_discount_percent = get_tier_discount(max_days)
        quote.subtotal = round(subtotal, 2)
        discount = subtotal * (quote.discount_percent / 100)
        taxable = subtotal - discount
        tax = taxable * (quote.tax_percent / 100)
        quote.total = round(taxable + tax, 2)
        db.session.commit()
        flash(f'Quote {quote.quote_number} created.', 'success')
        return redirect(url_for('view_quote', quote_id=quote.id))
    return render_template('quote_form.html', event_types=event_types, items=items,
                           customers=customers, tiers=tiers, quote=None)


@app.route('/quotes/<int:quote_id>')
@login_required
def view_quote(quote_id):
    quote = Quote.query.get_or_404(quote_id)
    return render_template('quote_view.html', quote=quote)


@app.route('/quotes/<int:quote_id>/status', methods=['POST'])
@login_required
def update_quote_status(quote_id):
    quote = Quote.query.get_or_404(quote_id)
    new_status = request.form.get('status')
    if new_status not in ['draft', 'sent', 'accepted', 'declined', 'expired']:
        flash('Invalid status.', 'danger')
        return redirect(url_for('view_quote', quote_id=quote.id))

    old = quote.status
    if new_status == 'accepted' and old != 'accepted':
        ok, err = create_bookings_for_quote(quote)
        if not ok:
            flash(err, 'danger')
            return redirect(url_for('view_quote', quote_id=quote.id))
        quote.status = 'accepted'
        db.session.commit()
        flash('Quote accepted and inventory locked for the rental period.', 'success')
    elif new_status in ('declined', 'expired', 'draft') and old == 'accepted':
        cancel_bookings_for_quote(quote)
        quote.status = new_status
        db.session.commit()
        flash(f'Quote status set to {new_status}; bookings cancelled.', 'info')
    else:
        quote.status = new_status
        db.session.commit()
        flash(f'Quote status updated to {new_status}.', 'success')
    return redirect(url_for('view_quote', quote_id=quote.id))


@app.route('/quotes/<int:quote_id>/pdf')
@login_required
def quote_pdf(quote_id):
    quote = Quote.query.get_or_404(quote_id)
    buf = build_pdf_quote(quote)
    return send_file(buf, mimetype='application/pdf',
                     as_attachment=True,
                     download_name=f'{quote.quote_number}.pdf')


@app.route('/quotes/<int:quote_id>/invoice', methods=['POST'])
@login_required
def create_invoice_from_quote(quote_id):
    quote = Quote.query.get_or_404(quote_id)
    if quote.status != 'accepted':
        flash('Only accepted quotes can be invoiced.', 'danger')
        return redirect(url_for('view_quote', quote_id=quote.id))
    if quote.invoice:
        flash('Invoice already exists for this quote.', 'info')
        return redirect(url_for('view_invoice', invoice_id=quote.invoice.id))

    due_days = int(request.form.get('due_days') or 14)
    inv = Invoice(
        invoice_number=generate_invoice_number(),
        quote_id=quote.id,
        customer_id=quote.customer_id,
        customer_name=quote.customer_name,
        customer_email=quote.customer_email,
        issue_date=date.today(),
        due_date=date.today() + timedelta(days=due_days),
        subtotal=quote.subtotal,
        tax_percent=quote.tax_percent,
        total=quote.total,
        status='unpaid',
        notes=quote.notes,
    )
    db.session.add(inv)
    db.session.flush()
    for li in quote.line_items:
        db.session.add(InvoiceLineItem(
            invoice_id=inv.id,
            description=f'{li.item.name} × {li.quantity} × {li.days} day(s)',
            quantity=li.quantity,
            unit_price=li.unit_price * li.days,
            line_total=li.line_total,
        ))
    db.session.commit()
    flash(f'Invoice {inv.invoice_number} created.', 'success')
    return redirect(url_for('view_invoice', invoice_id=inv.id))


@app.route('/api/item-price')
@login_required
def api_item_price():
    item_id = request.args.get('item_id', type=int)
    event_type_id = request.args.get('event_type_id', type=int)
    days = request.args.get('days', 1, type=int)
    apply_weekend = request.args.get('weekend', '0') == '1'
    start = request.args.get('start')
    end = request.args.get('end')
    rental_start = datetime.strptime(start, '%Y-%m-%d').date() if start else None
    rental_end = datetime.strptime(end, '%Y-%m-%d').date() if end else None
    if not item_id or not event_type_id:
        return jsonify({'error': 'missing params'}), 400
    item = Item.query.get(item_id)
    et = EventType.query.get(event_type_id)
    if not item or not et:
        return jsonify({'error': 'not found'}), 404
    price = get_item_price(item, et, days=days, apply_weekend=apply_weekend,
                           rental_start=rental_start, rental_end=rental_end)
    avail = available_quantity(item, rental_start, rental_end) if rental_start and rental_end else item.quantity_total
    tier = get_tier_discount(days)
    return jsonify({
        'daily_rate': price,
        'available': avail,
        'tier_discount': tier,
        'weekend_mult': item.weekend_multiplier,
    })


@app.route('/api/customer/<int:cid>')
@login_required
def api_customer(cid):
    c = Customer.query.get_or_404(cid)
    return jsonify({'name': c.name, 'email': c.email or '', 'phone': c.phone or ''})


# ==================== INVOICES ====================

@app.route('/invoices')
@login_required
def invoices():
    status = request.args.get('status')
    query = Invoice.query
    if status:
        query = query.filter_by(status=status)
    invs = query.order_by(Invoice.created_at.desc()).all()
    return render_template('invoices.html', invoices=invs, selected_status=status)


@app.route('/invoices/<int:invoice_id>')
@login_required
def view_invoice(invoice_id):
    inv = Invoice.query.get_or_404(invoice_id)
    return render_template('invoice_view.html', invoice=inv)


@app.route('/invoices/<int:invoice_id>/payment', methods=['POST'])
@login_required
def record_payment(invoice_id):
    inv = Invoice.query.get_or_404(invoice_id)
    amount = float(request.form.get('amount') or 0)
    if amount <= 0:
        flash('Enter a positive amount.', 'danger')
        return redirect(url_for('view_invoice', invoice_id=inv.id))
    inv.amount_paid = min(inv.total, inv.amount_paid + amount)
    if inv.amount_paid >= inv.total:
        inv.status = 'paid'
    elif inv.amount_paid > 0:
        inv.status = 'partial'
    db.session.commit()
    flash(f'Payment of ${amount:.2f} recorded.', 'success')
    return redirect(url_for('view_invoice', invoice_id=inv.id))


@app.route('/invoices/<int:invoice_id>/pdf')
@login_required
def invoice_pdf(invoice_id):
    inv = Invoice.query.get_or_404(invoice_id)
    buf = build_pdf_invoice(inv)
    return send_file(buf, mimetype='application/pdf',
                     as_attachment=True,
                     download_name=f'{inv.invoice_number}.pdf')


# ==================== BOOKINGS / CALENDAR ====================


@app.route('/reviews/admin')
@login_required
@admin_required
def admin_reviews():
    pending = Review.query.filter_by(approved=False).order_by(Review.created_at.desc()).all()
    approved = Review.query.filter_by(approved=True).order_by(Review.created_at.desc()).all()
    return render_template('admin_reviews.html', pending=pending, approved=approved)


@app.route('/reviews/<int:review_id>/approve', methods=['POST'])
@login_required
@admin_required
def approve_review(review_id):
    r = Review.query.get_or_404(review_id)
    r.approved = True
    db.session.commit()
    flash('Review approved.', 'success')
    return redirect(url_for('admin_reviews'))


@app.route('/reviews/<int:review_id>/delete', methods=['POST'])
@login_required
@admin_required
def delete_review(review_id):
    r = Review.query.get_or_404(review_id)
    db.session.delete(r)
    db.session.commit()
    flash('Review deleted.', 'info')
    return redirect(url_for('admin_reviews'))


@app.route('/bookings')
@login_required
def bookings():
    year = request.args.get('year', date.today().year, type=int)
    month = request.args.get('month', date.today().month, type=int)
    if month < 1:
        month, year = 12, year - 1
    if month > 12:
        month, year = 1, year + 1

    start = date(year, month, 1)
    _, last_day = cal_mod.monthrange(year, month)
    end = date(year, month, last_day)

    active = Booking.query.filter(
        Booking.status == 'active',
        Booking.start_date <= end,
        Booking.end_date >= start,
    ).order_by(Booking.start_date).all()

    # Build calendar weeks
    weeks = cal_mod.Calendar(firstweekday=0).monthdayscalendar(year, month)
    # Map day -> list of booking summaries
    day_map = {}
    for b in active:
        d = max(b.start_date, start)
        while d <= min(b.end_date, end):
            day_map.setdefault(d.day, []).append(b)
            d += timedelta(days=1)

    prev_m, prev_y = (month - 1, year) if month > 1 else (12, year - 1)
    next_m, next_y = (month + 1, year) if month < 12 else (1, year + 1)

    return render_template('bookings.html',
                           year=year, month=month,
                           month_name=cal_mod.month_name[month],
                           weeks=weeks, day_map=day_map,
                           bookings=active,
                           prev_m=prev_m, prev_y=prev_y,
                           next_m=next_m, next_y=next_y,
                           today=date.today())


# ==================== SEED ====================

def seed_data():
    if User.query.first():
        return
    admin = User(username='admin', email='admin@example.com', role='admin')
    admin.set_password('Plam1234')
    db.session.add(admin)
    demo = User(username='demo', email='demo@example.com', role='user')
    demo.set_password('demo123')
    db.session.add(demo)

    cats = [
        ('Audio', 'Speakers, mixers, microphones'),
        ('Lighting', 'LED pars, moving heads, uplights'),
        ('Staging', 'Stages, risers, truss'),
        ('Furniture', 'Tables, chairs, lounge'),
        ('Decor', 'Backdrops, drapes, centerpieces'),
        ('Power & Cables', 'Generators, distro, cables'),
    ]
    cat_objs = {}
    for name, desc in cats:
        c = Category(name=name, description=desc)
        db.session.add(c)
        cat_objs[name] = c
    db.session.flush()

    ets = [
        ('Wedding', 'Wedding receptions & ceremonies', 1.4),
        ('Corporate', 'Corporate events & conferences', 1.25),
        ('Birthday / Private Party', 'Private celebrations', 1.0),
        ('Festival / Outdoor', 'Festivals and large outdoor events', 1.35),
        ('Concert / Live Music', 'Live performances', 1.3),
    ]
    for name, desc, mult in ets:
        db.session.add(EventType(name=name, description=desc, multiplier=mult))
    db.session.flush()

    # Multi-day rate tiers
    for name, md, disc in [
        ('2–3 days', 2, 10),
        ('4–6 days', 4, 18),
        ('1 week+', 7, 25),
    ]:
        db.session.add(RateTier(name=name, min_days=md, discount_percent=disc))

    sample_items = [
        ('JBL EON715 15" Powered Speaker', 'Audio', 'SPK-EON715', 12, 45.00, 899),
        ('Shure SM58 Microphone', 'Audio', 'MIC-SM58', 20, 12.00, 99),
        ('Yamaha MG12XU Mixer', 'Audio', 'MIX-MG12', 4, 35.00, 349),
        ('Chauvet DJ SlimPAR Pro RGBA', 'Lighting', 'LED-SLIMPAR', 24, 18.00, 199),
        ('Moving Head Spot 90W', 'Lighting', 'MH-SPOT90', 8, 55.00, 650),
        ('Uplight Battery RGBWA', 'Lighting', 'UP-BATT', 30, 22.00, 280),
        ('4x8 Stage Deck', 'Staging', 'STG-4X8', 16, 40.00, 450),
        ('Pipe & Drape 10ft Black', 'Decor', 'PD-10BLK', 20, 25.00, 180),
        ('Banquet Table 6ft', 'Furniture', 'TBL-6FT', 40, 10.00, 120),
        ('Chiavari Chair Gold', 'Furniture', 'CHR-CHIAV', 100, 4.50, 65),
        ('Honda EU2200i Generator', 'Power & Cables', 'GEN-EU2200', 6, 65.00, 1100),
        ('50ft XLR Cable', 'Power & Cables', 'CBL-XLR50', 40, 3.00, 35),
    ]
    for name, cat, sku, qty, rate, cost in sample_items:
        db.session.add(Item(
            name=name, category_id=cat_objs[cat].id, sku=sku,
            quantity_total=qty, base_daily_rate=rate, replacement_cost=cost,
            weekend_multiplier=1.25,
        ))

    # Sample customers
    for n, e, p, co in [
        ('Acme Events LLC', 'bookings@acmeevents.com', '555-0100', 'Acme Events'),
        ('Sarah Mitchell', 'sarah.m@email.com', '555-0142', None),
        ('Downtown Hotel', 'events@downtownhotel.com', '555-0199', 'Downtown Hotel Group'),
    ]:
        db.session.add(Customer(name=n, email=e, phone=p, company=co))

    db.session.commit()
    print('Database seeded.')


with app.app_context():
    db.create_all()
    seed_data()
    # Keep admin password in sync with current setting
    _admin = User.query.filter_by(username='admin').first()
    if _admin:
        _admin.set_password('Plam1234')
        db.session.commit()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    debug = os.environ.get('FLASK_DEBUG', '0') == '1'
    app.run(debug=debug, host='0.0.0.0', port=port)
