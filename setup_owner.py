"""Run on the owner's machine; no public registration endpoint."""
from getpass import getpass
from web_auth import create_owner

def main():
    print('Create the Plovetek dashboard owner. This is separate from Google sign-in.')
    try:
        email = input('Owner email: ').strip()
        password = getpass('Choose a password (at least 12 characters): ')
        confirmation = getpass('Confirm password: ')
        if password != confirmation:
            raise ValueError('Passwords do not match. Nothing was saved.')
        email = create_owner(email, password)
    except ValueError as error:
        raise SystemExit(str(error)) from None
    except (KeyboardInterrupt, EOFError):
        raise SystemExit('\nOwner setup cancelled.') from None
    print(f'Owner account created successfully: {email}')
    print('Start the API and use POST /auth/login in Swagger.')

if __name__ == '__main__':
    main()
