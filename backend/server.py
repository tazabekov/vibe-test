from fastapi import FastAPI, APIRouter, Depends, HTTPException, status, Body, Query, Path, UploadFile, File
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from dotenv import load_dotenv
from starlette.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
import os
import logging
from pathlib import Path
from pydantic import BaseModel, Field, EmailStr
from typing import List, Optional, Dict, Any, Union
import uuid
from datetime import datetime, timedelta
import jwt
from passlib.context import CryptContext
from google.oauth2 import id_token
from google.auth.transport import requests as google_requests

# Directory and environment setup
ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

# MongoDB connection
mongo_url = os.environ['MONGO_URL']
client = AsyncIOMotorClient(mongo_url)
db = client[os.environ.get('DB_NAME', 'localhands_db')]

# Create the main app without a prefix
app = FastAPI(title="LocalHands.shop API", description="API for the LocalHands marketplace")

# Create a router with the /api prefix
api_router = APIRouter(prefix="/api")

# Constants
SECRET_KEY = os.environ.get("SECRET_KEY", "localhandssecretkey123456789")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24 * 7  # 1 week
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")

# Password hashing
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/token")

# User related models
class UserRole(str):
    CUSTOMER = "customer"
    SHOP_ADMIN = "admin"
    SUPERADMIN = "superadmin"

class UserBase(BaseModel):
    email: EmailStr
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    profile_pic: Optional[str] = None

class UserCreate(UserBase):
    password: Optional[str] = None
    role: str = UserRole.CUSTOMER

class UserLogin(BaseModel):
    email: EmailStr
    password: str

class User(UserBase):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    role: str = UserRole.CUSTOMER
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    is_active: bool = True
    shops: List[str] = []  # List of shop IDs the user is admin of

class TokenData(BaseModel):
    email: Optional[str] = None
    role: Optional[str] = None
    sub: Optional[str] = None
    exp: Optional[int] = None

class Token(BaseModel):
    access_token: str
    token_type: str
    user: User

class GoogleAuthRequest(BaseModel):
    token: str

# Shop related models
class DeliverySettings(BaseModel):
    offers_delivery: bool = True
    offers_pickup: bool = True
    max_distance: Optional[float] = None  # in kilometers
    delivery_fee: Optional[float] = None
    free_delivery_threshold: Optional[float] = None

class ShopBase(BaseModel):
    name: str
    slug: str
    description: str
    logo: Optional[str] = None
    banner: Optional[str] = None
    delivery_settings: DeliverySettings = DeliverySettings()
    contact_email: Optional[EmailStr] = None
    contact_phone: Optional[str] = None
    address: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    postal_code: Optional[str] = None
    country: Optional[str] = None
    location: Optional[Dict[str, float]] = None  # {lat, lng}

class ShopCreate(ShopBase):
    pass

class Shop(ShopBase):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    admin_ids: List[str] = []  # List of user IDs who can manage this shop
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    is_active: bool = True

# Product related models
class ProductBase(BaseModel):
    name: str
    description: str
    price: float
    sale_price: Optional[float] = None
    images: List[str] = []
    category: str
    inventory: Optional[int] = None
    is_available: bool = True
    tags: List[str] = []

class ProductCreate(ProductBase):
    shop_id: str

class Product(ProductBase):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    shop_id: str
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

# Order related models
class OrderItem(BaseModel):
    product_id: str
    name: str
    price: float
    quantity: int
    total: float

class DeliveryInfo(BaseModel):
    method: str  # "delivery" or "pickup"
    address: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    postal_code: Optional[str] = None
    country: Optional[str] = None
    delivery_fee: float = 0.0

class OrderBase(BaseModel):
    shop_id: str
    user_id: str
    items: List[OrderItem]
    subtotal: float
    delivery_info: DeliveryInfo
    total: float
    payment_method: str = "mock"  # For now, just use mock
    payment_status: str = "pending"  # pending, paid, failed

class OrderCreate(OrderBase):
    pass

class Order(OrderBase):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    status: str = "pending"  # pending, processing, shipped, delivered, cancelled
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

# Auth related functions
def verify_password(plain_password, hashed_password):
    return pwd_context.verify(plain_password, hashed_password)

def get_password_hash(password):
    return pwd_context.hash(password)

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None):
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt

async def get_user(email: str):
    if (user := await db.users.find_one({"email": email})):
        return User(**user)
    return None

async def get_user_by_id(user_id: str):
    if (user := await db.users.find_one({"id": user_id})):
        return User(**user)
    return None

async def authenticate_user(email: str, password: str):
    user = await get_user(email)
    if not user:
        return False
    # Check if the user was created via Google auth (no password)
    if not await db.users.find_one({"email": email, "password": {"$exists": True}}):
        return False
    user_doc = await db.users.find_one({"email": email})
    if not verify_password(password, user_doc["password"]):
        return False
    return user

async def get_current_user(token: str = Depends(oauth2_scheme)):
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        email: str = payload.get("sub")
        if email is None:
            raise credentials_exception
        token_data = TokenData(email=email, role=payload.get("role"))
    except jwt.PyJWTError:
        raise credentials_exception
    user = await get_user(email=token_data.email)
    if user is None:
        raise credentials_exception
    return user

async def get_current_active_user(current_user: User = Depends(get_current_user)):
    if not current_user.is_active:
        raise HTTPException(status_code=400, detail="Inactive user")
    return current_user

async def get_current_active_admin(current_user: User = Depends(get_current_user)):
    if not current_user.is_active:
        raise HTTPException(status_code=400, detail="Inactive user")
    if current_user.role not in [UserRole.SHOP_ADMIN, UserRole.SUPERADMIN]:
        raise HTTPException(
            status_code=403, detail="Not enough permissions"
        )
    return current_user

async def get_current_active_superadmin(current_user: User = Depends(get_current_user)):
    if not current_user.is_active:
        raise HTTPException(status_code=400, detail="Inactive user")
    if current_user.role != UserRole.SUPERADMIN:
        raise HTTPException(
            status_code=403, detail="Not enough permissions"
        )
    return current_user

# Authentication routes
@api_router.post("/auth/register", response_model=Token)
async def register_user(user_create: UserCreate):
    existing_user = await get_user(user_create.email)
    if existing_user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email already registered"
        )
    
    user_dict = user_create.dict()
    if user_create.password:
        hashed_password = get_password_hash(user_create.password)
        user_dict["password"] = hashed_password
    
    user_obj = User(**user_dict)
    user_document = user_obj.dict()
    await db.users.insert_one(user_document)
    
    access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = create_access_token(
        data={"sub": user_obj.email, "role": user_obj.role},
        expires_delta=access_token_expires
    )
    return {"access_token": access_token, "token_type": "bearer", "user": user_obj}

@api_router.post("/auth/token", response_model=Token)
async def login_for_access_token(form_data: OAuth2PasswordRequestForm = Depends()):
    user = await authenticate_user(form_data.username, form_data.password)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = create_access_token(
        data={"sub": user.email, "role": user.role},
        expires_delta=access_token_expires
    )
    return {"access_token": access_token, "token_type": "bearer", "user": user}

@api_router.post("/auth/google", response_model=Token)
async def login_with_google(google_auth: GoogleAuthRequest):
    try:
        idinfo = id_token.verify_oauth2_token(
            google_auth.token, google_requests.Request(), GOOGLE_CLIENT_ID)
            
        # Or, if multiple clients access the backend:
        # idinfo = id_token.verify_oauth2_token(token, google_requests.Request())
        # if idinfo['aud'] not in CLIENT_ID_LIST:
        #     raise ValueError('Could not verify audience.')

        # ID token is valid. Get the user's Google Account ID and email:
        email = idinfo['email']
        
        # Check if user exists
        user = await get_user(email)
        if not user:
            # Create new user
            user_data = UserCreate(
                email=email,
                first_name=idinfo.get('given_name'),
                last_name=idinfo.get('family_name'),
                profile_pic=idinfo.get('picture'),
                role=UserRole.CUSTOMER
            )
            user_obj = User(**user_data.dict())
            await db.users.insert_one(user_obj.dict())
            user = user_obj
        
        # Create access token
        access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
        access_token = create_access_token(
            data={"sub": user.email, "role": user.role},
            expires_delta=access_token_expires
        )
        return {"access_token": access_token, "token_type": "bearer", "user": user}
        
    except ValueError:
        # Invalid token
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid Google token",
            headers={"WWW-Authenticate": "Bearer"},
        )

@api_router.get("/users/me", response_model=User)
async def get_user_me(current_user: User = Depends(get_current_active_user)):
    return current_user

# Shop management routes
@api_router.post("/shops", response_model=Shop)
async def create_shop(shop_create: ShopCreate, current_user: User = Depends(get_current_active_admin)):
    # Check if shop slug already exists
    existing_shop = await db.shops.find_one({"slug": shop_create.slug})
    if existing_shop:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Shop slug already exists"
        )
    
    shop_obj = Shop(**shop_create.dict(), admin_ids=[current_user.id])
    shop_document = shop_obj.dict()
    await db.shops.insert_one(shop_document)
    
    # Add shop to user's shops
    await db.users.update_one(
        {"id": current_user.id},
        {"$push": {"shops": shop_obj.id}}
    )
    
    return shop_obj

@api_router.get("/shops", response_model=List[Shop])
async def get_shops(
    skip: int = 0, 
    limit: int = 20, 
    active_only: bool = True,
    category: Optional[str] = None
):
    query = {"is_active": True} if active_only else {}
    if category:
        # Find shops that have products in this category
        shop_ids = await db.products.distinct("shop_id", {"category": category})
        query["id"] = {"$in": shop_ids}
    
    shops = await db.shops.find(query).skip(skip).limit(limit).to_list(limit)
    return [Shop(**shop) for shop in shops]

@api_router.get("/shops/{slug}", response_model=Shop)
async def get_shop_by_slug(slug: str):
    shop = await db.shops.find_one({"slug": slug})
    if not shop:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Shop not found"
        )
    return Shop(**shop)

@api_router.put("/shops/{shop_id}", response_model=Shop)
async def update_shop(
    shop_id: str,
    shop_data: ShopBase,
    current_user: User = Depends(get_current_active_user)
):
    # Check if shop exists and user is admin
    shop = await db.shops.find_one({"id": shop_id})
    if not shop:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Shop not found"
        )
    
    if current_user.role != UserRole.SUPERADMIN and current_user.id not in shop["admin_ids"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not authorized to update this shop"
        )
    
    # Update shop
    shop_dict = shop_data.dict(exclude_unset=True)
    shop_dict["updated_at"] = datetime.utcnow()
    
    await db.shops.update_one(
        {"id": shop_id},
        {"$set": shop_dict}
    )
    
    updated_shop = await db.shops.find_one({"id": shop_id})
    return Shop(**updated_shop)

@api_router.post("/shops/{shop_id}/admins", response_model=Shop)
async def add_shop_admin(
    shop_id: str,
    admin_email: str = Body(..., embed=True),
    current_user: User = Depends(get_current_active_admin)
):
    # Check if shop exists
    shop = await db.shops.find_one({"id": shop_id})
    if not shop:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Shop not found"
        )
    
    # Check if current user is shop admin or superadmin
    if current_user.role != UserRole.SUPERADMIN and current_user.id not in shop["admin_ids"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not authorized to add admins to this shop"
        )
    
    # Find user by email
    user_to_add = await get_user(admin_email)
    if not user_to_add:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found"
        )
    
    # Update user role if they're not already an admin
    if user_to_add.role == UserRole.CUSTOMER:
        await db.users.update_one(
            {"id": user_to_add.id},
            {"$set": {"role": UserRole.SHOP_ADMIN}}
        )
    
    # Add user to shop admins if not already an admin
    if user_to_add.id not in shop["admin_ids"]:
        await db.shops.update_one(
            {"id": shop_id},
            {"$push": {"admin_ids": user_to_add.id}}
        )
        
        # Add shop to user's shops
        await db.users.update_one(
            {"id": user_to_add.id},
            {"$push": {"shops": shop_id}}
        )
    
    updated_shop = await db.shops.find_one({"id": shop_id})
    return Shop(**updated_shop)

@api_router.delete("/shops/{shop_id}/admins/{user_id}", response_model=Shop)
async def remove_shop_admin(
    shop_id: str,
    user_id: str,
    current_user: User = Depends(get_current_active_admin)
):
    # Only superadmin can remove admins, or if there are multiple admins
    shop = await db.shops.find_one({"id": shop_id})
    if not shop:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Shop not found"
        )
    
    # Check if current user is authorized
    if current_user.role != UserRole.SUPERADMIN and current_user.id not in shop["admin_ids"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not authorized to remove admins from this shop"
        )
    
    # Prevent removing the last admin
    if len(shop["admin_ids"]) <= 1 and user_id in shop["admin_ids"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot remove the last admin from the shop"
        )
    
    # Remove admin from shop
    await db.shops.update_one(
        {"id": shop_id},
        {"$pull": {"admin_ids": user_id}}
    )
    
    # Remove shop from user's shops
    await db.users.update_one(
        {"id": user_id},
        {"$pull": {"shops": shop_id}}
    )
    
    updated_shop = await db.shops.find_one({"id": shop_id})
    return Shop(**updated_shop)

# Product routes
@api_router.post("/products", response_model=Product)
async def create_product(
    product_create: ProductCreate,
    current_user: User = Depends(get_current_active_admin)
):
    # Check if shop exists and user is admin
    shop = await db.shops.find_one({"id": product_create.shop_id})
    if not shop:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Shop not found"
        )
    
    if current_user.role != UserRole.SUPERADMIN and current_user.id not in shop["admin_ids"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not authorized to add products to this shop"
        )
    
    product_obj = Product(**product_create.dict())
    product_document = product_obj.dict()
    await db.products.insert_one(product_document)
    
    return product_obj

@api_router.get("/products", response_model=List[Product])
async def get_products(
    shop_id: Optional[str] = None,
    category: Optional[str] = None,
    available_only: bool = True,
    search: Optional[str] = None,
    skip: int = 0,
    limit: int = 20
):
    query = {}
    if shop_id:
        query["shop_id"] = shop_id
    if category:
        query["category"] = category
    if available_only:
        query["is_available"] = True
    if search:
        query["$or"] = [
            {"name": {"$regex": search, "$options": "i"}},
            {"description": {"$regex": search, "$options": "i"}},
            {"tags": {"$in": [search]}}
        ]
    
    products = await db.products.find(query).skip(skip).limit(limit).to_list(limit)
    return [Product(**product) for product in products]

@api_router.get("/products/{product_id}", response_model=Product)
async def get_product(product_id: str):
    product = await db.products.find_one({"id": product_id})
    if not product:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Product not found"
        )
    return Product(**product)

@api_router.put("/products/{product_id}", response_model=Product)
async def update_product(
    product_id: str,
    product_data: ProductBase,
    current_user: User = Depends(get_current_active_admin)
):
    # Check if product exists
    product = await db.products.find_one({"id": product_id})
    if not product:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Product not found"
        )
    
    # Check if user is shop admin
    shop = await db.shops.find_one({"id": product["shop_id"]})
    if current_user.role != UserRole.SUPERADMIN and current_user.id not in shop["admin_ids"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not authorized to update this product"
        )
    
    # Update product
    product_dict = product_data.dict(exclude_unset=True)
    product_dict["updated_at"] = datetime.utcnow()
    
    await db.products.update_one(
        {"id": product_id},
        {"$set": product_dict}
    )
    
    updated_product = await db.products.find_one({"id": product_id})
    return Product(**updated_product)

@api_router.delete("/products/{product_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_product(
    product_id: str,
    current_user: User = Depends(get_current_active_admin)
):
    # Check if product exists
    product = await db.products.find_one({"id": product_id})
    if not product:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Product not found"
        )
    
    # Check if user is shop admin
    shop = await db.shops.find_one({"id": product["shop_id"]})
    if current_user.role != UserRole.SUPERADMIN and current_user.id not in shop["admin_ids"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not authorized to delete this product"
        )
    
    # Delete product
    await db.products.delete_one({"id": product_id})
    
    return None

# Order routes
@api_router.post("/orders", response_model=Order)
async def create_order(
    order_create: OrderCreate,
    current_user: User = Depends(get_current_active_user)
):
    # Validate order (ensure products exist and belong to the shop)
    shop = await db.shops.find_one({"id": order_create.shop_id})
    if not shop:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Shop not found"
        )
    
    for item in order_create.items:
        product = await db.products.find_one({
            "id": item.product_id,
            "shop_id": order_create.shop_id
        })
        if not product:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Product {item.product_id} not found in this shop"
            )
        
        # Check if product is available
        if not product["is_available"]:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Product {product['name']} is not available"
            )
        
        # Check inventory if applicable
        if product["inventory"] is not None and product["inventory"] < item.quantity:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Not enough inventory for {product['name']}"
            )
    
    # Create order
    order_obj = Order(**order_create.dict())
    order_document = order_obj.dict()
    await db.orders.insert_one(order_document)
    
    # Update product inventory
    for item in order_create.items:
        product = await db.products.find_one({"id": item.product_id})
        if product["inventory"] is not None:
            new_inventory = product["inventory"] - item.quantity
            await db.products.update_one(
                {"id": item.product_id},
                {"$set": {"inventory": new_inventory}}
            )
    
    return order_obj

@api_router.get("/orders", response_model=List[Order])
async def get_orders(
    current_user: User = Depends(get_current_active_user),
    shop_id: Optional[str] = None,
    status: Optional[str] = None,
    skip: int = 0,
    limit: int = 20
):
    query = {}
    
    # If normal user, only show their orders
    if current_user.role == UserRole.CUSTOMER:
        query["user_id"] = current_user.id
    # If shop admin, only show orders from their shops
    elif current_user.role == UserRole.SHOP_ADMIN:
        if shop_id and shop_id in current_user.shops:
            query["shop_id"] = shop_id
        else:
            query["shop_id"] = {"$in": current_user.shops}
    # Superadmin can see all orders or filter by shop
    elif shop_id:
        query["shop_id"] = shop_id
        
    if status:
        query["status"] = status
    
    orders = await db.orders.find(query).skip(skip).limit(limit).to_list(limit)
    return [Order(**order) for order in orders]

@api_router.get("/orders/{order_id}", response_model=Order)
async def get_order(
    order_id: str,
    current_user: User = Depends(get_current_active_user)
):
    order = await db.orders.find_one({"id": order_id})
    if not order:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Order not found"
        )
    
    # Check if user is authorized to view this order
    if current_user.role == UserRole.CUSTOMER and order["user_id"] != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not authorized to view this order"
        )
    
    if current_user.role == UserRole.SHOP_ADMIN and order["shop_id"] not in current_user.shops:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not authorized to view this order"
        )
    
    return Order(**order)

@api_router.put("/orders/{order_id}/status", response_model=Order)
async def update_order_status(
    order_id: str,
    status: str = Body(..., embed=True),
    current_user: User = Depends(get_current_active_admin)
):
    # Validate status
    valid_statuses = ["pending", "processing", "shipped", "delivered", "cancelled"]
    if status not in valid_statuses:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid status. Must be one of {valid_statuses}"
        )
    
    # Check if order exists
    order = await db.orders.find_one({"id": order_id})
    if not order:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Order not found"
        )
    
    # Check if user is authorized to update this order
    if current_user.role == UserRole.SHOP_ADMIN and order["shop_id"] not in current_user.shops:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not authorized to update this order"
        )
    
    # Update order status
    await db.orders.update_one(
        {"id": order_id},
        {"$set": {"status": status, "updated_at": datetime.utcnow()}}
    )
    
    updated_order = await db.orders.find_one({"id": order_id})
    return Order(**updated_order)

# Superadmin routes
@api_router.put("/users/{user_id}/role", response_model=User)
async def update_user_role(
    user_id: str,
    role: str = Body(..., embed=True),
    current_user: User = Depends(get_current_active_superadmin)
):
    # Validate role
    valid_roles = [UserRole.CUSTOMER, UserRole.SHOP_ADMIN, UserRole.SUPERADMIN]
    if role not in valid_roles:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid role. Must be one of {valid_roles}"
        )
    
    # Check if user exists
    user = await get_user_by_id(user_id)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found"
        )
    
    # Update user role
    await db.users.update_one(
        {"id": user_id},
        {"$set": {"role": role, "updated_at": datetime.utcnow()}}
    )
    
    updated_user = await get_user_by_id(user_id)
    return updated_user

@api_router.delete("/shops/{shop_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_shop(
    shop_id: str,
    current_user: User = Depends(get_current_active_superadmin)
):
    # Check if shop exists
    shop = await db.shops.find_one({"id": shop_id})
    if not shop:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Shop not found"
        )
    
    # Delete shop
    await db.shops.delete_one({"id": shop_id})
    
    # Remove shop from all users
    await db.users.update_many(
        {"shops": shop_id},
        {"$pull": {"shops": shop_id}}
    )
    
    # Delete all products from this shop
    await db.products.delete_many({"shop_id": shop_id})
    
    return None

# Categories
@api_router.get("/categories", response_model=List[str])
async def get_categories():
    categories = await db.products.distinct("category")
    return categories

# Include the router in the main app
app.include_router(api_router)

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

@app.on_event("shutdown")
async def shutdown_db_client():
    client.close()
