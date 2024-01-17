import sys
import random
from getpass import getuser
from pathlib import Path

def generate_key(length=10):
    """Generate a random key of given length"""
    chars = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789!@#$%^&*()-_=+[]{}|;,.<>?/~'
    return ''.join(random.choice(chars) for _ in range(length))

def add_user(users_list=None):
    """Add a new user to the users list file"""
    user_name = input('Enter your username: ')
    key = generate_key()
    print(f'Your key is: {user_name}:{key}')
    if not users_list or not users_list.exists():
        users_list = Path(users_list) if users_list else Path('authorized_users.txt')
        users_list.touch(exist_ok=True)
    with open(users_list, 'a') as f:
        f.write(f'{user_name}:{key}\n')
    print(f'User {user_name} added to the authorized users list')

def main():
    if len(sys.argv) > 1 and sys.argv[1] == '--users_list':
        add_user(Path(sys.argv[2]))
    else:
        add_user()

if __name__ == '__main__':
    main()
