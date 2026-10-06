import os, jwt
from datetime import datetime, timedelta, timezone
from fastapi import FastAPI, HTTPException, Depends, Header
from pydantic import BaseModel, Field
from sqlalchemy import create_engine, Column, Integer, String, Text, Boolean, DateTime, ForeignKey, or_
from sqlalchemy.orm import declarative_base, sessionmaker, Session
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

DB_URL=os.getenv("DURA_DATABASE_URL","sqlite:///./dura_cloud.db")
SECRET=os.getenv("DURA_SECRET","CHANGE-ME-BEFORE-PUBLICATION")
engine=create_engine(DB_URL,connect_args={"check_same_thread":False} if DB_URL.startswith("sqlite") else {})
SessionLocal=sessionmaker(bind=engine,autoflush=False,autocommit=False)
Base=declarative_base(); ph=PasswordHasher()

class User(Base):
    __tablename__="users"
    id=Column(Integer,primary_key=True); address=Column(String(255),unique=True,index=True,nullable=False)
    display_name=Column(String(80),nullable=False); channel_name=Column(String(80),nullable=False)
    password_hash=Column(String(255),nullable=False); is_admin=Column(Boolean,default=False)
    is_official=Column(Boolean,default=False); is_banned=Column(Boolean,default=False)
    created_at=Column(DateTime,default=datetime.utcnow)

class Mail(Base):
    __tablename__="mail"
    id=Column(Integer,primary_key=True); sender_id=Column(Integer,ForeignKey("users.id"),nullable=False)
    recipient_id=Column(Integer,ForeignKey("users.id"),nullable=False); subject=Column(String(180),default="")
    body=Column(Text,default=""); is_read=Column(Boolean,default=False); is_starred=Column(Boolean,default=False)
    trash_sender=Column(Boolean,default=False); trash_recipient=Column(Boolean,default=False)
    created_at=Column(DateTime,default=datetime.utcnow)

Base.metadata.create_all(engine)
app=FastAPI(title="Dura Cloud",version="1.0")

def db():
    x=SessionLocal()
    try: yield x
    finally: x.close()

def clean_address(a):
    a=a.strip().lower()
    if "@" not in a: a+="@duramail"
    if not a.endswith("@duramail"): raise HTTPException(400,"Adresse @duramail obligatoire.")
    return a

def token(u):
    return jwt.encode({"sub":str(u.id),"exp":datetime.now(timezone.utc)+timedelta(days=30)},SECRET,algorithm="HS256")

def me(authorization:str=Header(default=""),d:Session=Depends(db)):
    if not authorization.startswith("Bearer "): raise HTTPException(401,"Connexion requise.")
    try: uid=int(jwt.decode(authorization[7:],SECRET,algorithms=["HS256"])["sub"])
    except Exception: raise HTTPException(401,"Session invalide.")
    u=d.get(User,uid)
    if not u or u.is_banned: raise HTTPException(403,"Compte indisponible.")
    return u

def public(u):
    return {"id":u.id,"address":u.address,"name":u.display_name,"channel":u.channel_name,
            "admin":bool(u.is_admin),"official":bool(u.is_official)}

class PublicRegister(BaseModel):
    address:str
    display_name:str=Field(min_length=2,max_length=80)
    channel_name:str=Field(min_length=2,max_length=80)
    password:str=Field(min_length=8,max_length=128)

class Login(BaseModel): address:str; password:str
class CreateUser(BaseModel):
    address:str; display_name:str=Field(min_length=2,max_length=80)
    channel_name:str=Field(min_length=2,max_length=80); password:str=Field(min_length=8,max_length=128)
class SendMail(BaseModel): to:str; subject:str=""; body:str=""

@app.on_event("startup")
def bootstrap():
    d=SessionLocal()
    try:
        if not d.query(User).filter(User.address=="admin@duramail").first():
            pw=os.getenv("DURA_ADMIN_PASSWORD","Dura-Admin-ChangeMe-2026!")
            d.add(User(address="admin@duramail",display_name="DuraIndustry",channel_name="DuraTube",
                       password_hash=ph.hash(pw),is_admin=True,is_official=True)); d.commit()
    finally:d.close()

@app.get("/")
def status(): return {"service":"Dura Cloud","version":"1.0","status":"online"}

@app.post("/auth/register")
def public_register(x:PublicRegister,d:Session=Depends(db)):
    a=clean_address(x.address)
    if d.query(User).filter(User.address==a).first(): raise HTTPException(409,"Cette adresse @duramail existe déjà.")
    channel=x.channel_name.strip()
    if d.query(User).filter(User.channel_name.ilike(channel)).first(): raise HTTPException(409,"Ce nom de chaîne est déjà utilisé.")
    nu=User(address=a,display_name=x.display_name.strip(),channel_name=channel,password_hash=ph.hash(x.password),is_admin=False,is_official=False)
    d.add(nu);d.commit();d.refresh(nu)
    official=d.query(User).filter(User.is_official==True).first()
    if official and official.id!=nu.id:
        d.add(Subscription(subscriber_id=nu.id,channel_id=official.id));d.commit()
    return {"token":make_token(nu),"user":pub_user(nu)}

@app.post("/auth/login")
def login(x:Login,d:Session=Depends(db)):
    u=d.query(User).filter(User.address==clean_address(x.address)).first()
    if not u: raise HTTPException(401,"Adresse ou mot de passe incorrect.")
    try: ok=ph.verify(u.password_hash,x.password)
    except VerifyMismatchError: ok=False
    if not ok: raise HTTPException(401,"Adresse ou mot de passe incorrect.")
    if u.is_banned: raise HTTPException(403,"Compte suspendu.")
    return {"token":token(u),"user":public(u)}

@app.get("/auth/me")
def who(u:User=Depends(me)): return public(u)

@app.post("/admin/users")
def create_user(x:CreateUser,u:User=Depends(me),d:Session=Depends(db)):
    if not u.is_admin: raise HTTPException(403,"Réservé à DuraIndustry.")
    a=clean_address(x.address)
    if d.query(User).filter(User.address==a).first(): raise HTTPException(409,"Adresse déjà utilisée.")
    nu=User(address=a,display_name=x.display_name.strip(),channel_name=x.channel_name.strip(),
            password_hash=ph.hash(x.password))
    d.add(nu);d.commit();d.refresh(nu);return public(nu)

@app.get("/admin/users")
def users(u:User=Depends(me),d:Session=Depends(db)):
    if not u.is_admin: raise HTTPException(403)
    return [public(x)|{"banned":bool(x.is_banned)} for x in d.query(User).order_by(User.id.desc()).all()]

@app.post("/mail")
def send_mail(x:SendMail,u:User=Depends(me),d:Session=Depends(db)):
    r=d.query(User).filter(User.address==clean_address(x.to)).first()
    if not r: raise HTTPException(404,"Cette adresse DuraMail n'existe pas.")
    m=Mail(sender_id=u.id,recipient_id=r.id,subject=x.subject[:180],body=x.body[:100000])
    d.add(m);d.commit();return {"ok":True}

@app.get("/mail")
def get_mail(folder:str="inbox",u:User=Depends(me),d:Session=Depends(db)):
    if folder=="sent":
        rows=d.query(Mail).filter(Mail.sender_id==u.id,Mail.trash_sender==False).order_by(Mail.id.desc()).all()
    elif folder=="trash":
        rows=d.query(Mail).filter(or_(
            (Mail.sender_id==u.id)&(Mail.trash_sender==True),
            (Mail.recipient_id==u.id)&(Mail.trash_recipient==True)
        )).order_by(Mail.id.desc()).all()
    else:
        rows=d.query(Mail).filter(Mail.recipient_id==u.id,Mail.trash_recipient==False).order_by(Mail.id.desc()).all()
    out=[]
    for m in rows:
        s=d.get(User,m.sender_id);r=d.get(User,m.recipient_id)
        out.append({"id":m.id,"from":s.address,"to":r.address,"peer":r.address if m.sender_id==u.id else s.address,
                    "subject":m.subject,"body":m.body,"read":bool(m.is_read),"starred":bool(m.is_starred),
                    "date":m.created_at.strftime("%d/%m/%Y %H:%M")})
    return out

@app.post("/mail/{mid}/read")
def mark_read(mid:int,u:User=Depends(me),d:Session=Depends(db)):
    m=d.get(Mail,mid)
    if not m or m.recipient_id!=u.id: raise HTTPException(404)
    m.is_read=True;d.commit();return {"ok":True}

@app.post("/mail/{mid}/star")
def star(mid:int,u:User=Depends(me),d:Session=Depends(db)):
    m=d.get(Mail,mid)
    if not m or u.id not in (m.sender_id,m.recipient_id): raise HTTPException(404)
    m.is_starred=not m.is_starred;d.commit();return {"starred":m.is_starred}

@app.delete("/mail/{mid}")
def trash(mid:int,u:User=Depends(me),d:Session=Depends(db)):
    m=d.get(Mail,mid)
    if not m: raise HTTPException(404)
    if m.sender_id==u.id:m.trash_sender=True
    if m.recipient_id==u.id:m.trash_recipient=True
    d.commit();return {"ok":True}
