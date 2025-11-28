# reset_admin_password.py
import asyncio
import getpass
import sys

from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.future import select

# This is a standalone script, so we need to add the app directory to the path
# to be able to import app modules.
sys.path.append('.')

from app.core.config import settings
from app.core.security import get_password_hash, pwd_context
from app.database.models import User

# --- Colors for output ---
COLOR_RESET = '\033[0m'
COLOR_INFO = '\033[1;34m'
COLOR_SUCCESS = '\033[1;32m'
COLOR_ERROR = '\033[1;31m'

def print_info(message):
    print(f"{COLOR_INFO}[INFO]{COLOR_RESET} {message}")

def print_success(message):
    print(f"{COLOR_SUCCESS}[SUCCESS]{COLOR_RESET} {message}")

def print_error(message):
    print(f"{COLOR_ERROR}[ERROR]{COLOR_RESET} {message}", file=sys.stderr)


async def main():
    """
    Asynchronous main function to handle the password reset logic.
    """
    print_info("Connecting to the database...")
    try:
        engine = create_async_engine(settings.DATABASE_URL)
        AsyncSessionLocal = async_sessionmaker(autocommit=False, autoflush=False, bind=engine)
    except Exception as e:
        print_error(f"Failed to configure database connection: {e}")
        print_error("Please ensure your .env file is present and DATABASE_URL is correct.")
        return

    # --- Get User Input ---
    admin_username = input(f"   -> Enter the username of the admin account to reset [default: {settings.ADMIN_USER}]: ").strip()
    if not admin_username:
        admin_username = settings.ADMIN_USER

    while True:
        new_password = getpass.getpass("   -> Enter the new password (will be hidden): ").strip()
        if not new_password:
            print_error("   Password cannot be empty. Please try again.")
            continue

        confirm_password = getpass.getpass("   -> Confirm the new password: ").strip()

        if new_password != confirm_password:
            print_error("   Passwords do not match. Please try again.")
        else:
            break

    # --- Database Operation ---
    async with AsyncSessionLocal() as db:
        print_info(f"Searching for admin user '{admin_username}'...")
        
        result = await db.execute(select(User).filter(User.username == admin_username))
        admin_user = result.scalars().first()

        if not admin_user:
            print_error(f"User '{admin_username}' not found in the database.")
            return

        if not admin_user.is_admin:
            print_error(f"User '{admin_username}' is not an admin account. Password not changed.")
            return
            
        print_info(f"Admin user '{admin_username}' found. Updating password...")
        
        # Check if the hash needs to be updated (e.g., from an old scheme)
        needs_update = pwd_context.needs_update(admin_user.hashed_password)
        
        # Hash the new password and update the user object
        admin_user.hashed_password = get_password_hash(new_password)
        
        try:
            await db.commit()
            print_success(f"Password for admin user '{admin_username}' has been successfully reset.")
            if needs_update:
                print_info("The password hash was also updated to the latest security scheme.")
        except Exception as e:
            await db.rollback()
            print_error(f"An error occurred while updating the database: {e}")


if __name__ == "__main__":
    print("\n==============================================")
    print("    Ollama Proxy Fortress - Admin Password Reset")
    print("==============================================")
    
    # Check for .env file
    try:
        from app.core.config import settings
    except ImportError:
        print_error("Could not find the .env file or essential configuration.")
        print_error("Please run this script from the project's root directory.")
        sys.exit(1)

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nOperation cancelled by user.")
    except Exception as e:
        print_error(f"An unexpected error occurred: {e}")