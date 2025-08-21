import os
from flask import Flask, render_template, redirect, url_for, flash, request, jsonify, session
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import func
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from flask_bcrypt import Bcrypt
from datetime import datetime, timedelta
from flask_wtf import FlaskForm
from wtforms import StringField, PasswordField, SubmitField, SelectField, FloatField, BooleanField
from flask_mail import Mail, Message
from flask_babel import Babel, _
from threading import Thread
from wtforms.validators import DataRequired, Email, EqualTo, ValidationError, Length
from functools import wraps
import requests
import xml.etree.ElementTree as ET
import pyotp
import qrcode
import io
import base64

# --- App Initialization and Configuration ---
basedir = os.path.abspath(os.path.dirname(__file__))
app = Flask(__name__)
app.config['SECRET_KEY'] = 'a_very_secret_key' # Replace with a real secret key
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + os.path.join(basedir, 'exchange.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# --- Mail Configuration ---
app.config['MAIL_SERVER'] = os.environ.get('MAIL_SERVER', 'smtp.mailtrap.io')
app.config['MAIL_PORT'] = int(os.environ.get('MAIL_PORT', 2525))
app.config['MAIL_USE_TLS'] = os.environ.get('MAIL_USE_TLS', 'true').lower() in ['true', 'on', '1']
app.config['MAIL_USERNAME'] = os.environ.get('MAIL_USERNAME')
app.config['MAIL_PASSWORD'] = os.environ.get('MAIL_PASSWORD')
app.config['MAIL_DEFAULT_SENDER'] = os.environ.get('MAIL_DEFAULT_SENDER', 'noreply@exchange.com')
app.config['MAIL_SUPPRESS_SEND'] = app.config['TESTING']

# --- Babel Configuration ---
app.config['LANGUAGES'] = ['en', 'ar']
app.config['BABEL_DEFAULT_LOCALE'] = 'ar'
app.config['BABEL_DEFAULT_TIMEZONE'] = 'UTC'

# --- Extensions Initialization ---
db = SQLAlchemy(app)
bcrypt = Bcrypt(app)
login_manager = LoginManager(app)
mail = Mail(app)
babel = Babel(app)

@babel.localeselector
def get_locale():
    return session.get('language', request.accept_languages.best_match(app.config['LANGUAGES']))

login_manager.login_view = 'login' # Redirect to 'login' page if user is not logged in
login_manager.login_message_category = 'info'

# --- User Loader for Flask-Login ---
@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# --- Database Models ---
class User(db.Model, UserMixin):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(20), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(128), nullable=False)
    role = db.Column(db.String(10), nullable=False, default='user')
    # 2FA fields
    otp_secret = db.Column(db.String(16), nullable=True)
    otp_enabled = db.Column(db.Boolean, nullable=False, default=False)

    def set_password(self, password):
        self.password_hash = bcrypt.generate_password_hash(password).decode('utf-8')

    def check_password(self, password):
        return bcrypt.check_password_hash(self.password_hash, password)

    def get_id(self):
        return str(self.id)

class Currency(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(10), unique=True, nullable=False)
    name = db.Column(db.String(100), nullable=False)

class ExchangeRate(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    from_currency_code = db.Column(db.String(10), db.ForeignKey('currency.code'), nullable=False)
    to_currency_code = db.Column(db.String(10), db.ForeignKey('currency.code'), nullable=False)
    rate = db.Column(db.Float, nullable=False)
    from_currency = db.relationship('Currency', foreign_keys=[from_currency_code])
    to_currency = db.relationship('Currency', foreign_keys=[to_currency_code])

class Transaction(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    from_currency_code = db.Column(db.String(10), db.ForeignKey('currency.code'), nullable=False)
    to_currency_code = db.Column(db.String(10), db.ForeignKey('currency.code'), nullable=False)
    amount_from = db.Column(db.Float, nullable=False)
    amount_to = db.Column(db.Float, nullable=False)
    fee = db.Column(db.Float, nullable=False, default=0.0)
    timestamp = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    user = db.relationship('User', backref=db.backref('transactions', lazy=True))
    from_currency = db.relationship('Currency', foreign_keys=[from_currency_code])
    to_currency = db.relationship('Currency', foreign_keys=[to_currency_code])

class AppSettings(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(50), unique=True, nullable=False)
    value = db.Column(db.String(200), nullable=False)

class Balance(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    currency_code = db.Column(db.String(10), db.ForeignKey('currency.code'), nullable=False)
    amount = db.Column(db.Float, nullable=False, default=0.0)

    user = db.relationship('User', backref=db.backref('balances', lazy=True))
    currency = db.relationship('Currency', backref=db.backref('balances', lazy=True))
    __table_args__ = (db.UniqueConstraint('user_id', 'currency_code', name='_user_currency_uc'),)


# --- Forms ---
class RegistrationForm(FlaskForm):
    username = StringField(_('Username'), validators=[DataRequired(), Length(min=2, max=20)])
    email = StringField(_('Email'), validators=[DataRequired(), Email()])
    password = PasswordField(_('Password'), validators=[DataRequired()])
    confirm_password = PasswordField(_('Confirm Password'), validators=[DataRequired(), EqualTo('password')])
    submit = SubmitField(_('Sign Up'))

    def validate_username(self, username):
        user = User.query.filter_by(username=username.data).first()
        if user:
            raise ValidationError(_('That username is taken. Please choose a different one.'))

    def validate_email(self, email):
        user = User.query.filter_by(email=email.data).first()
        if user:
            raise ValidationError(_('That email is taken. Please choose a different one.'))

class LoginForm(FlaskForm):
    email = StringField(_('Email'), validators=[DataRequired(), Email()])
    password = PasswordField(_('Password'), validators=[DataRequired()])
    submit = SubmitField(_('Login'))

class CurrencyForm(FlaskForm):
    code = StringField(_('Currency Code (e.g., USD)'), validators=[DataRequired(), Length(min=3, max=10)])
    name = StringField(_('Currency Name (e.g., US Dollar)'), validators=[DataRequired(), Length(min=3, max=100)])
    submit = SubmitField(_('Save Currency'))

class ExchangeRateForm(FlaskForm):
    from_currency_code = SelectField(_('From Currency'), validators=[DataRequired()])
    to_currency_code = SelectField(_('To Currency'), validators=[DataRequired()])
    rate = FloatField(_('Rate'), validators=[DataRequired()])
    submit = SubmitField(_('Save Rate'))

class ExchangeForm(FlaskForm):
    from_currency = SelectField(_('I Pay With'), validators=[DataRequired()])
    to_currency = SelectField(_('I Receive'), validators=[DataRequired()])
    amount = FloatField(_('Amount'), validators=[DataRequired()])
    submit = SubmitField(_('Execute Exchange'))

class SettingsForm(FlaskForm):
    fee_percent = FloatField(_('Transaction Fee (%%)'), validators=[DataRequired()])
    live_rates_enabled = BooleanField(_('Enable Live Exchange Rates'))
    profit_margin_percent = FloatField(_('Profit Margin (%%)'))
    submit = SubmitField(_('Save Settings'))

class TwoFAEnableForm(FlaskForm):
    otp = StringField(_('Verification Code'), validators=[DataRequired(), Length(min=6, max=6)])
    submit = SubmitField(_('Verify and Enable'))

class TwoFAVerifyForm(FlaskForm):
    otp = StringField(_('Verification Code'), validators=[DataRequired(), Length(min=6, max=6)])
    submit = SubmitField(_('Verify'))


# --- Helper Functions ---
def get_balance(user_id, currency_code):
    balance = Balance.query.filter_by(user_id=user_id, currency_code=currency_code).first()
    return balance.amount if balance else 0.0

def get_setting(key, default=None):
    setting = AppSettings.query.filter_by(key=key).first()
    if setting:
        return setting.value
    return default

def send_async_email(app, msg):
    with app.app_context():
        mail.send(msg)

def send_email(to, subject, template, **kwargs):
    msg = Message(subject, recipients=[to])
    msg.html = render_template(template, **kwargs)
    thr = Thread(target=send_async_email, args=[app, msg])
    thr.start()
    return thr

# --- Decorators ---
def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated or current_user.role != 'admin':
            flash(_('You do not have permission to access this page.'), 'danger')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

# --- Routes ---
@app.route('/language/<language>')
def set_language(language=None):
    session['language'] = language
    return redirect(request.referrer)

@app.route('/')
@app.route('/home')
def home():
    form = ExchangeForm()
    currencies = Currency.query.all()
    form.from_currency.choices = [(c.code, c.code) for c in currencies]
    form.to_currency.choices = [(c.code, c.code) for c in currencies]
    return render_template('home.html', form=form)

@app.route('/register', methods=['GET', 'POST'])
def register():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    form = RegistrationForm()
    if form.validate_on_submit():
        user = User(username=form.username.data, email=form.email.data)
        user.set_password(form.password.data)
        # For simplicity, first user registered is an admin
        if User.query.count() == 0:
            user.role = 'admin'
        db.session.add(user)
        db.session.commit()

        # Add starting balance for the new user
        starting_balance = Balance(user_id=user.id, currency_code='USD', amount=1000.0)
        db.session.add(starting_balance)
        db.session.commit()

        # Send welcome email
        send_email(user.email, _('Welcome to the Exchange Platform!'), 'email/welcome.html', user=user)
        flash(_('Your account has been created! A starting balance of 1000 USD has been added to your wallet.'), 'success')
        return redirect(url_for('login'))
    return render_template('register.html', title=_('Register'), form=form)

@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    form = LoginForm()
    if form.validate_on_submit():
        user = User.query.filter_by(email=form.email.data).first()
        if user and user.check_password(form.password.data):
            # If 2FA is enabled, redirect to the 2FA verification page
            if user.otp_enabled:
                session['2fa_user_id'] = user.id
                return redirect(url_for('login_2fa'))

            # If 2FA is not enabled, log in directly
            login_user(user)
            next_page = request.args.get('next')
            flash(_('Login Successful!'), 'success')
            return redirect(next_page) if next_page else redirect(url_for('dashboard'))
        else:
            flash(_('Login Unsuccessful. Please check email and password'), 'danger')
    return render_template('login.html', title=_('Login'), form=form)

@app.route('/login/2fa', methods=['GET', 'POST'])
def login_2fa():
    if '2fa_user_id' not in session:
        return redirect(url_for('login'))

    form = TwoFAVerifyForm()
    if form.validate_on_submit():
        user = User.query.get(session['2fa_user_id'])
        totp = pyotp.TOTP(user.otp_secret)
        if totp.verify(form.otp.data):
            login_user(user)
            session.pop('2fa_user_id', None) # Clean up session
            next_page = request.args.get('next')
            flash(_('Login Successful!'), 'success')
            return redirect(next_page) if next_page else redirect(url_for('dashboard'))
        else:
            flash(_('Invalid verification code. Please try again.'), 'danger')

    return render_template('login_2fa.html', form=form)

@app.route('/logout')
def logout():
    logout_user()
    return redirect(url_for('home'))

@app.route('/dashboard', methods=['GET', 'POST'])
@login_required
def dashboard():
    if current_user.role == 'admin':
        return redirect(url_for('admin_dashboard'))

    form = ExchangeForm()
    currencies = Currency.query.all()
    form.from_currency.choices = [(c.code, c.code) for c in currencies]
    form.to_currency.choices = [(c.code, c.code) for c in currencies]

    if form.validate_on_submit():
        from_code = form.from_currency.data
        to_code = form.to_currency.data
        amount_to_exchange = form.amount.data

        # 1. Check user's balance
        user_balance = get_balance(current_user.id, from_code)
        if user_balance < amount_to_exchange:
            flash(_('Insufficient balance. You have %(balance)s %(currency)s.', balance=user_balance, currency=from_code), 'danger')
            return redirect(url_for('dashboard'))

        # 2. Get rate and calculate
        rate_obj = ExchangeRate.query.filter_by(from_currency_code=from_code, to_currency_code=to_code).first()
        if not rate_obj:
            flash(_('Exchange rate from %(from_code)s to %(to_code)s not found.', from_code=from_code, to_code=to_code), 'danger')
            return redirect(url_for('dashboard'))

        final_rate = rate_obj.rate
        if get_setting('live_rates_enabled', 'false') == 'true':
            profit_margin = float(get_setting('profit_margin_percent', 0.0))
            final_rate = final_rate * (1 - profit_margin / 100.0)

        fee_percent = float(get_setting('transaction_fee_percent', 0.0))
        fee_in_from_currency = (amount_to_exchange * fee_percent) / 100.0

        # Check if balance is sufficient for amount + fee
        if user_balance < amount_to_exchange + fee_in_from_currency:
             flash(_('Insufficient balance for exchange and fee. Total required: %(total)s %(currency)s.', total=(amount_to_exchange + fee_in_from_currency), currency=from_code), 'danger')
             return redirect(url_for('dashboard'))

        amount_to_receive = (amount_to_exchange - fee_in_from_currency) * final_rate

        # 3. Perform atomic balance update
        try:
            # Debit from 'from' balance
            from_balance = Balance.query.filter_by(user_id=current_user.id, currency_code=from_code).first()
            from_balance.amount -= amount_to_exchange

            # Credit to 'to' balance
            to_balance = Balance.query.filter_by(user_id=current_user.id, currency_code=to_code).first()
            if to_balance:
                to_balance.amount += amount_to_receive
            else:
                # Create a new balance if it doesn't exist
                to_balance = Balance(user_id=current_user.id, currency_code=to_code, amount=amount_to_receive)
                db.session.add(to_balance)

            # 4. Log the transaction
            transaction = Transaction(
                user_id=current_user.id,
                from_currency_code=from_code,
                to_currency_code=to_code,
                amount_from=amount_to_exchange,
                amount_to=amount_to_receive,
                fee=fee_in_from_currency * final_rate # Store fee in the 'to' currency for consistency
            )
            db.session.add(transaction)

            db.session.commit()

            # 5. Send notifications
            send_email(current_user.email, _('Your Transaction Receipt'), 'email/transaction_receipt.html', user=current_user, transaction=transaction)
            flash(_('Successfully exchanged %(amount_from)s %(from_code)s to %(amount_to)s %(to_code)s', amount_from=amount_to_exchange, from_code=from_code, amount_to=f'{amount_to_receive:.2f}', to_code=to_code), 'success')
            return redirect(url_for('my_transactions'))

        except Exception as e:
            db.session.rollback()
            flash(_('An error occurred during the transaction: %(error)s', error=e), 'danger')
            return redirect(url_for('dashboard'))

    return render_template('dashboard.html', title=_('Dashboard'), form=form)

@app.route('/my-transactions')
@login_required
def my_transactions():
    transactions = Transaction.query.filter_by(user_id=current_user.id).order_by(Transaction.timestamp.desc()).all()
    return render_template('transactions.html', transactions=transactions)

@app.route('/wallet')
@login_required
def wallet():
    balances = Balance.query.filter_by(user_id=current_user.id).order_by(Balance.amount.desc()).all()
    return render_template('wallet.html', balances=balances)

@app.route('/account')
@login_required
def account():
    return render_template('account.html')

@app.route('/2fa/enable', methods=['GET', 'POST'])
@login_required
def enable_2fa():
    form = TwoFAEnableForm()
    if current_user.otp_enabled:
        flash(_('2FA is already enabled.'), 'info')
        return redirect(url_for('account'))

    if form.validate_on_submit():
        totp = pyotp.TOTP(current_user.otp_secret)
        if totp.verify(form.otp.data):
            current_user.otp_enabled = True
            db.session.commit()
            flash(_('2FA enabled successfully!'), 'success')
            return redirect(url_for('account'))
        else:
            flash(_('Invalid verification code.'), 'danger')

    if not current_user.otp_secret:
        current_user.otp_secret = pyotp.random_base32()
        db.session.commit()

    totp_uri = pyotp.totp.TOTP(current_user.otp_secret).provisioning_uri(
        name=current_user.email,
        issuer_name='ExchangeApp'
    )
    img = qrcode.make(totp_uri)
    buf = io.BytesIO()
    img.save(buf)
    buf.seek(0)
    qr_code_data = base64.b64encode(buf.getvalue()).decode('ascii')

    return render_template('enable_2fa.html', form=form, qr_code_data=qr_code_data)

@app.route('/2fa/disable', methods=['POST'])
@login_required
def disable_2fa():
    current_user.otp_enabled = False
    current_user.otp_secret = None
    db.session.commit()
    flash(_('2FA has been disabled.'), 'success')
    return redirect(url_for('account'))

@app.route('/api/balance/<string:currency_code>')
@login_required
def get_user_balance(currency_code):
    balance = get_balance(current_user.id, currency_code)
    return jsonify({'balance': balance})


@app.route('/api/calculate', methods=['POST'])
def calculate():
    data = request.get_json()
    from_code = data.get('from_currency')
    to_code = data.get('to_currency')
    amount = data.get('amount')

    if not all([from_code, to_code, amount]):
        return jsonify({'error': 'Missing data'}), 400

    try:
        amount = float(amount)
    except ValueError:
        return jsonify({'error': 'Invalid amount'}), 400

    if from_code == to_code:
        return jsonify({
            'result': amount,
            'fee': 0,
            'rate': 1
        })

    rate_obj = ExchangeRate.query.filter_by(from_currency_code=from_code, to_currency_code=to_code).first()
    if not rate_obj:
        return jsonify({'error': 'Rate not found'}), 404

    # Live rate and profit margin logic
    final_rate = rate_obj.rate
    if get_setting('live_rates_enabled', 'false') == 'true':
        profit_margin = float(get_setting('profit_margin_percent', 0.0))
        final_rate = final_rate * (1 - profit_margin / 100.0) # Subtract margin for user

    # Fee calculation
    fee_percent = float(get_setting('transaction_fee_percent', 0.0))
    fee_in_from_currency = (amount * fee_percent) / 100.0

    amount_to_before_fee = amount * final_rate
    fee_in_to_currency = fee_in_from_currency * final_rate
    amount_to_after_fee = amount_to_before_fee - fee_in_to_currency

    return jsonify({
        'result': amount_to_after_fee,
        'fee': fee_in_to_currency,
        'rate': final_rate
    })


# --- Admin Routes ---
@app.route('/admin/dashboard')
@login_required
@admin_required
def admin_dashboard():
    return render_template('admin/dashboard.html', title='Admin Dashboard')

# --- Admin API Routes for Stats ---

@app.route('/api/admin/stats/key_metrics')
@login_required
@admin_required
def key_metrics():
    total_users = db.session.query(func.count(User.id)).scalar()
    total_transactions = db.session.query(func.count(Transaction.id)).scalar()
    # To get total volume, we need to normalize amounts to a single currency (e.g., EUR)
    # This is a complex step, so we'll just count transactions for now.

    return jsonify({
        'total_users': total_users,
        'total_transactions': total_transactions,
    })

@app.route('/api/admin/stats/transactions_over_time')
@login_required
@admin_required
def transactions_over_time():
    thirty_days_ago = datetime.utcnow() - timedelta(days=30)

    data = db.session.query(
        func.date(Transaction.timestamp),
        func.count(Transaction.id)
    ).filter(Transaction.timestamp >= thirty_days_ago).group_by(func.date(Transaction.timestamp)).order_by(func.date(Transaction.timestamp)).all()

    # Format data for Chart.js
    labels = [row[0].isoformat() for row in data]
    values = [row[1] for row in data]

    return jsonify({'labels': labels, 'values': values})

@app.route('/api/admin/stats/volume_by_currency')
@login_required
@admin_required
def volume_by_currency():
    # Summing up the 'from_amount' for each currency
    data = db.session.query(
        Transaction.from_currency_code,
        func.sum(Transaction.amount_from)
    ).group_by(Transaction.from_currency_code).order_by(func.sum(Transaction.amount_from).desc()).limit(10).all()

    labels = [row[0] for row in data]
    values = [row[1] for row in data]

    return jsonify({'labels': labels, 'values': values})


@app.route('/admin/currencies', methods=['GET', 'POST'])
@login_required
@admin_required
def manage_currencies():
    form = CurrencyForm()
    if form.validate_on_submit():
        new_currency = Currency(code=form.code.data.upper(), name=form.name.data)
        db.session.add(new_currency)
        db.session.commit()
        flash(_('Currency added successfully!'), 'success')
        return redirect(url_for('manage_currencies'))

    currencies = Currency.query.all()
    return render_template('admin/currencies.html', title=_('Manage Currencies'), form=form, currencies=currencies)

@app.route('/admin/currency/edit/<int:id>', methods=['GET', 'POST'])
@login_required
@admin_required
def edit_currency(id):
    currency = Currency.query.get_or_404(id)
    form = CurrencyForm(obj=currency)
    if form.validate_on_submit():
        currency.code = form.code.data.upper()
        currency.name = form.name.data
        db.session.commit()
        flash(_('Currency updated successfully!'), 'success')
        return redirect(url_for('manage_currencies'))

    return render_template('admin/edit_currency.html', title=_('Edit Currency'), form=form)

@app.route('/admin/currency/delete/<int:id>', methods=['POST'])
@login_required
@admin_required
def delete_currency(id):
    currency = Currency.query.get_or_404(id)
    # Add check for dependencies before deleting
    db.session.delete(currency)
    db.session.commit()
    flash(_('Currency deleted successfully!'), 'success')
    return redirect(url_for('manage_currencies'))

@app.route('/admin/rates', methods=['GET', 'POST'])
@login_required
@admin_required
def manage_rates():
    form = ExchangeRateForm()
    # Populate choices for the select fields
    currencies = Currency.query.all()
    form.from_currency_code.choices = [(c.code, c.code) for c in currencies]
    form.to_currency_code.choices = [(c.code, c.code) for c in currencies]

    if form.validate_on_submit():
        from_code = form.from_currency_code.data
        to_code = form.to_currency_code.data

        if from_code == to_code:
            flash(_('Cannot create an exchange rate for the same currency.'), 'danger')
        else:
            # Check if rate already exists, if so, update it
            existing_rate = ExchangeRate.query.filter_by(from_currency_code=from_code, to_currency_code=to_code).first()
            if existing_rate:
                existing_rate.rate = form.rate.data
                flash(_('Exchange rate updated successfully!'), 'success')
            else:
                new_rate = ExchangeRate(
                    from_currency_code=from_code,
                    to_currency_code=to_code,
                    rate=form.rate.data
                )
                db.session.add(new_rate)
                flash(_('Exchange rate added successfully!'), 'success')

            db.session.commit()
            return redirect(url_for('manage_rates'))

    rates = ExchangeRate.query.all()
    return render_template('admin/rates.html', title=_('Manage Exchange Rates'), form=form, rates=rates)

@app.route('/admin/rate/delete/<int:id>', methods=['POST'])
@login_required
@admin_required
def delete_rate(id):
    rate = ExchangeRate.query.get_or_404(id)
    db.session.delete(rate)
    db.session.commit()
    flash(_('Exchange rate deleted successfully!'), 'success')
    return redirect(url_for('manage_rates'))

@app.route('/admin/settings', methods=['GET', 'POST'])
@login_required
@admin_required
def manage_settings():
    form = SettingsForm()

    if form.validate_on_submit():
        AppSettings.query.filter_by(key='transaction_fee_percent').first().value = str(form.fee_percent.data)
        AppSettings.query.filter_by(key='live_rates_enabled').first().value = str(form.live_rates_enabled.data).lower()
        AppSettings.query.filter_by(key='profit_margin_percent').first().value = str(form.profit_margin_percent.data)
        db.session.commit()
        flash(_('Settings updated successfully!'), 'success')
        return redirect(url_for('manage_settings'))

    form.fee_percent.data = float(get_setting('transaction_fee_percent', 0.0))
    form.live_rates_enabled.data = get_setting('live_rates_enabled', 'false') == 'true'
    form.profit_margin_percent.data = float(get_setting('profit_margin_percent', 0.0))

    return render_template('admin/settings.html', title=_('Manage Settings'), form=form)

def update_rates_from_ecb():
    """Fetches and updates rates from the European Central Bank feed."""
    try:
        url = 'https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml'
        response = requests.get(url, timeout=10)
        response.raise_for_status()

        tree = ET.fromstring(response.content)
        # Namespace for ECB feed
        ns = {'ecb': 'http://www.ecb.int/vocabulary/2002-08-01/eurofxref'}

        live_rates_vs_eur = {
            'EUR': 1.0 # Add EUR as the base
        }

        # Extract all rates vs EUR from the XML
        for cube in tree.findall('.//ecb:Cube[@currency]', ns):
            currency_code = cube.get('currency')
            rate = float(cube.get('rate'))
            live_rates_vs_eur[currency_code] = rate

        # Update the rates in our database
        all_rates_in_db = ExchangeRate.query.all()
        updated_count = 0
        for rate_in_db in all_rates_in_db:
            from_code = rate_in_db.from_currency_code
            to_code = rate_in_db.to_currency_code

            # Check if we have the live rates for the required currencies
            if from_code in live_rates_vs_eur and to_code in live_rates_vs_eur:
                # Calculate the cross rate
                # (Rate of To-Currency vs EUR) / (Rate of From-Currency vs EUR)
                cross_rate = live_rates_vs_eur[to_code] / live_rates_vs_eur[from_code]
                rate_in_db.rate = cross_rate
                updated_count += 1

        db.session.commit()
        return (True, _('Successfully updated %(num)s exchange rates.', num=updated_count))

    except Exception as e:
        return (False, _('An error occurred: %(error)s', error=e))


@app.route('/admin/update-live-rates', methods=['POST'])
@login_required
@admin_required
def update_live_rates():
    success, message = update_rates_from_ecb()
    if success:
        flash(message, 'success')
    else:
        flash(message, 'danger')
    return redirect(url_for('manage_rates'))


if __name__ == '__main__':
    with app.app_context():
        db.create_all()
        # Ensure default settings and currencies are in the database
        if not get_setting('transaction_fee_percent'):
            db.session.add(AppSettings(key='transaction_fee_percent', value='0.0'))
        if not get_setting('live_rates_enabled'):
            db.session.add(AppSettings(key='live_rates_enabled', value='false'))
        if not get_setting('profit_margin_percent'):
            db.session.add(AppSettings(key='profit_margin_percent', value='0.0'))

        if not Currency.query.filter_by(code='USD').first():
            db.session.add(Currency(code='USD', name='US Dollar'))
        if not Currency.query.filter_by(code='EUR').first():
            db.session.add(Currency(code='EUR', name='Euro'))

        db.session.commit()
    app.run(debug=True)
