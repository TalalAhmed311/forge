class AuthError(Exception):
    pass


def login(username: str, password: str) -> str:
    # Dummy user data for demonstration purposes
    dummy_user = {
        'username': 'testuser',
        'password': 'testpass'
    }

    if username == dummy_user['username'] and password == dummy_user['password']:
        # Return a dummy token for successful authentication
        return 'dummy_token'
    else:
        # Raise an error for invalid credentials
        raise ValueError('Invalid credentials')
