import unittest
import os
import json
from app import app, db, User, Currency, ExchangeRate, Transaction

class AppTestCase(unittest.TestCase):
    def setUp(self):
        """Set up a new test client and a new database."""
        basedir = os.path.abspath(os.path.dirname(__file__))
        self.db_path = os.path.join(basedir, 'test.db')
        app.config['TESTING'] = True
        app.config['WTF_CSRF_ENABLED'] = False
        app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + self.db_path
        self.client = app.test_client()
        with app.app_context():
            db.create_all()

    def tearDown(self):
        """Tear down all initialized variables."""
        with app.app_context():
            db.session.remove()
            db.drop_all()
        if os.path.exists(self.db_path):
            os.unlink(self.db_path)

    def register_and_login(self, username, email, password, role='user'):
        """Helper to register and login a user."""
        with app.app_context():
            user = User(username=username, email=email, role=role)
            user.set_password(password)
            db.session.add(user)
            db.session.commit()

        return self.client.post(
            '/login',
            data=dict(email=email, password=password),
            follow_redirects=True
        )

    def logout(self):
        """Helper function to log out."""
        return self.client.get('/logout', follow_redirects=True)

    # --- Test Cases ---

    def test_home_page(self):
        """Test that the home page loads correctly."""
        response = self.client.get('/')
        self.assertEqual(response.status_code, 200)
        self.assertIn('أهلاً بك في نظام الصرافة'.encode('utf-8'), response.data)

    def test_registration_and_login(self):
        """Test user registration and login."""
        response = self.register_and_login('testuser', 'test@example.com', 'password123')
        self.assertEqual(response.status_code, 200)
        self.assertIn('تم تسجيل الدخول بنجاح!'.encode('utf-8'), response.data)
        self.assertIn('حاسبة الصرف'.encode('utf-8'), response.data)

    def test_logout(self):
        """Test user logout."""
        self.register_and_login('testuser', 'test@example.com', 'password123')
        response = self.logout()
        self.assertIn('أهلاً بك في نظام الصرافة'.encode('utf-8'), response.data)

    def test_admin_access(self):
        """Test that only admins can access admin pages."""
        # Test with regular user
        self.register_and_login('testuser', 'test@example.com', 'password123', role='user')
        response = self.client.get('/admin/dashboard', follow_redirects=True)
        self.assertNotIn('لوحة تحكم المدير'.encode('utf-8'), response.data)
        self.assertIn('You do not have permission'.encode('utf-8'), response.data)
        self.logout()

        # Test with admin user
        self.register_and_login('adminuser', 'admin@example.com', 'password123', role='admin')
        response = self.client.get('/admin/dashboard', follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn('لوحة تحكم المدير'.encode('utf-8'), response.data)

    def test_currency_management(self):
        """Test adding a currency as an admin."""
        self.register_and_login('admin', 'admin@example.com', 'password123', role='admin')
        response = self.client.post('/admin/currencies', data={
            'code': 'USD',
            'name': 'US Dollar'
        }, follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn('Currency added successfully!'.encode('utf-8'), response.data)
        with app.app_context():
            currency = Currency.query.filter_by(code='USD').first()
            self.assertIsNotNone(currency)
            self.assertEqual(currency.name, 'US Dollar')

    def test_rate_management(self):
        """Test adding an exchange rate as an admin."""
        self.register_and_login('admin', 'admin@example.com', 'password123', role='admin')
        # First add currencies
        self.client.post('/admin/currencies', data={'code': 'USD', 'name': 'US Dollar'})
        self.client.post('/admin/currencies', data={'code': 'EUR', 'name': 'Euro'})

        # Then add the rate
        response = self.client.post('/admin/rates', data={
            'from_currency_code': 'USD',
            'to_currency_code': 'EUR',
            'rate': 0.95
        }, follow_redirects=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn('Exchange rate added successfully!'.encode('utf-8'), response.data)
        with app.app_context():
            rate = ExchangeRate.query.filter_by(from_currency_code='USD').first()
            self.assertIsNotNone(rate)
            self.assertEqual(rate.rate, 0.95)

    def test_exchange_api_and_transaction(self):
        """Test the exchange calculation API and creating a transaction."""
        # Setup as admin
        self.register_and_login('admin', 'admin@example.com', 'password123', role='admin')
        self.client.post('/admin/currencies', data={'code': 'USD', 'name': 'US Dollar'})
        self.client.post('/admin/currencies', data={'code': 'JPY', 'name': 'Japanese Yen'})
        self.client.post('/admin/rates', data={'from_currency_code': 'USD', 'to_currency_code': 'JPY', 'rate': 150.0})
        self.logout()

        # Action as user
        self.register_and_login('user', 'user@example.com', 'password123', role='user')

        # Test API
        response = self.client.post('/api/calculate', json={
            'from_currency': 'USD',
            'to_currency': 'JPY',
            'amount': 10
        })
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.data)
        self.assertAlmostEqual(data['result'], 1500.0)

        # Test creating a transaction
        response = self.client.post('/dashboard', data={
            'from_currency': 'USD',
            'to_currency': 'JPY',
            'amount': 100
        }, follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn('Successfully exchanged'.encode('utf-8'), response.data)
        with app.app_context():
            tx = Transaction.query.first()
            self.assertIsNotNone(tx)
            self.assertEqual(tx.amount_from, 100)
            self.assertAlmostEqual(tx.amount_to, 15000.0)


if __name__ == '__main__':
    unittest.main()
