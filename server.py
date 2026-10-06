import os, uuid, jwt, boto3, random, hashlib, ast, math, re, html, secrets, logging
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from fastapi import FastAPI, HTTPException, Depends, Header, UploadFile, File, Form
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import create_engine, Column, Integer, String, Text, Boolean, DateTime, ForeignKey, or_, UniqueConstraint, text
from sqlalchemy.orm import declarative_base, sessionmaker, Session
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

logger=logging.getLogger("duracloud")
logging.basicConfig(level=os.getenv("LOG_LEVEL","INFO"))

CONFIG_WARNINGS=[]

def normalize_database_url(url:str)->str:
    url=(url or "sqlite:///./dura_cloud.db").strip()
    if url.startswith("postgres://"):
        return "postgresql+psycopg://"+url[len("postgres://"):]
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://"+url[len("postgresql://"):]
    return url

DB_URL=normalize_database_url(os.getenv("DURA_DATABASE_URL","sqlite:///./dura_cloud.db"))
_raw_secret=os.getenv("DURA_SECRET","").strip()
if not _raw_secret or _raw_secret=="CHANGE-ME":
    SECRET=secrets.token_urlsafe(48)
    CONFIG_WARNINGS.append("DURA_SECRET absent: secret temporaire généré. Ajoute DURA_SECRET dans Render pour conserver les sessions après redémarrage.")
elif len(_raw_secret)<32:
    SECRET=_raw_secret
    CONFIG_WARNINGS.append("DURA_SECRET est trop court. Le serveur démarre, mais utilise au moins 32 caractères avant publication.")
else:
    SECRET=_raw_secret

R2_ENDPOINT=os.getenv("R2_ENDPOINT","").strip().rstrip("/")
R2_ACCESS_KEY_ID=os.getenv("R2_ACCESS_KEY_ID","").strip()
R2_SECRET_ACCESS_KEY=os.getenv("R2_SECRET_ACCESS_KEY","").strip()
R2_BUCKET=os.getenv("R2_BUCKET_NAME","duratube-media").strip()
MAX_VIDEO_MB=int(os.getenv("MAX_VIDEO_MB","2048"))
MAX_POST_MEDIA_MB=int(os.getenv("MAX_POST_MEDIA_MB","100"))
MAX_MAIL_ATTACHMENT_MB=int(os.getenv("MAX_MAIL_ATTACHMENT_MB","25"))

engine=create_engine(DB_URL,connect_args={"check_same_thread":False} if DB_URL.startswith("sqlite") else {},pool_pre_ping=True,pool_recycle=300)
SessionLocal=sessionmaker(bind=engine,autoflush=False,autocommit=False)
Base=declarative_base(); ph=PasswordHasher()
DATABASE_READY=False

class User(Base):
    __tablename__="users"
    id=Column(Integer,primary_key=True); address=Column(String(255),unique=True,index=True,nullable=False)
    display_name=Column(String(80),nullable=False); channel_name=Column(String(80),nullable=False,default="")
    password_hash=Column(String(255),nullable=False); is_admin=Column(Boolean,default=False)
    is_official=Column(Boolean,default=False); is_banned=Column(Boolean,default=False)
    created_at=Column(DateTime,default=datetime.utcnow)
class Mail(Base):
    __tablename__="mail"
    id=Column(Integer,primary_key=True); sender_id=Column(Integer,ForeignKey("users.id"),nullable=False)
    recipient_id=Column(Integer,ForeignKey("users.id"),nullable=False); subject=Column(String(180),default="")
    body=Column(Text,default=""); is_read=Column(Boolean,default=False); is_starred=Column(Boolean,default=False)
    trash_sender=Column(Boolean,default=False); trash_recipient=Column(Boolean,default=False); created_at=Column(DateTime,default=datetime.utcnow)
class Video(Base):
    __tablename__="videos"
    id=Column(Integer,primary_key=True); owner_id=Column(Integer,ForeignKey("users.id"),nullable=False,index=True)
    title=Column(String(180),nullable=False); description=Column(Text,default=""); channel=Column(String(80),nullable=False)
    object_key=Column(String(500),nullable=False); thumbnail_key=Column(String(500),nullable=True)
    is_short=Column(Boolean,default=False); status=Column(String(30),default="published")
    views=Column(Integer,default=0); likes=Column(Integer,default=0); is_featured=Column(Boolean,default=False); created_at=Column(DateTime,default=datetime.utcnow)
class VideoLike(Base):
    __tablename__="video_likes"; __table_args__=(UniqueConstraint("video_id","user_id",name="uq_video_like"),)
    id=Column(Integer,primary_key=True); video_id=Column(Integer,ForeignKey("videos.id"),nullable=False,index=True); user_id=Column(Integer,ForeignKey("users.id"),nullable=False,index=True)
class Subscription(Base):
    __tablename__="subscriptions"; __table_args__=(UniqueConstraint("subscriber_id","channel_id",name="uq_subscription"),)
    id=Column(Integer,primary_key=True); subscriber_id=Column(Integer,ForeignKey("users.id"),nullable=False,index=True); channel_id=Column(Integer,ForeignKey("users.id"),nullable=False,index=True)
class Comment(Base):
    __tablename__="comments"
    id=Column(Integer,primary_key=True); video_id=Column(Integer,ForeignKey("videos.id"),nullable=False,index=True); user_id=Column(Integer,ForeignKey("users.id"),nullable=False)
    content=Column(Text,nullable=False); created_at=Column(DateTime,default=datetime.utcnow)
class Post(Base):
    __tablename__="posts"
    id=Column(Integer,primary_key=True); user_id=Column(Integer,ForeignKey("users.id"),nullable=False,index=True); content=Column(Text,default="")
    media_key=Column(String(500),nullable=True); media_type=Column(String(20),default="none"); likes=Column(Integer,default=0); created_at=Column(DateTime,default=datetime.utcnow)
class PostLike(Base):
    __tablename__="post_likes"; __table_args__=(UniqueConstraint("post_id","user_id",name="uq_post_like"),)
    id=Column(Integer,primary_key=True); post_id=Column(Integer,ForeignKey("posts.id"),nullable=False); user_id=Column(Integer,ForeignKey("users.id"),nullable=False)
class Promotion(Base):
    __tablename__="promotions"
    id=Column(Integer,primary_key=True); user_id=Column(Integer,ForeignKey("users.id"),nullable=False); video_id=Column(Integer,ForeignKey("videos.id"),nullable=False)
    budget_cents=Column(Integer,default=0); target_impressions=Column(Integer,default=0); delivered_impressions=Column(Integer,default=0); status=Column(String(30),default="active"); created_at=Column(DateTime,default=datetime.utcnow)
class ChannelProfile(Base):
    __tablename__="channel_profiles"
    id=Column(Integer,primary_key=True); user_id=Column(Integer,ForeignKey("users.id"),unique=True,nullable=False)
    description=Column(Text,default=""); logo_key=Column(String(500),nullable=True); banner_key=Column(String(500),nullable=True)
    accent=Column(String(20),default="#7c5cff"); font=Column(String(80),default="Segoe UI"); layout=Column(String(30),default="modern")
class ThemeProfile(Base):
    __tablename__="theme_profiles"
    id=Column(Integer,primary_key=True); user_id=Column(Integer,ForeignKey("users.id"),unique=True,nullable=False)
    accent=Column(String(20),default="#ff3158"); background=Column(String(20),default="#0f0f10"); surface=Column(String(20),default="#18181b")
    text=Column(String(20),default="#f6f6f7"); font=Column(String(80),default="Segoe UI"); radius=Column(Integer,default=16); density=Column(String(20),default="comfortable"); graphic=Column(String(30),default="clean")
class AiAccess(Base):
    __tablename__="ai_access"
    id=Column(Integer,primary_key=True); user_id=Column(Integer,ForeignKey("users.id"),unique=True,nullable=False); verified=Column(Boolean,default=False); verified_at=Column(DateTime,nullable=True)
class AiCode(Base):
    __tablename__="ai_codes"
    id=Column(Integer,primary_key=True); user_id=Column(Integer,ForeignKey("users.id"),nullable=False,index=True); code_hash=Column(String(64),nullable=False); expires_at=Column(DateTime,nullable=False); used=Column(Boolean,default=False)
class AiConversation(Base):
    __tablename__="ai_conversations"
    id=Column(Integer,primary_key=True); user_id=Column(Integer,ForeignKey("users.id"),nullable=False,index=True); title=Column(String(120),default="Nouvelle conversation"); created_at=Column(DateTime,default=datetime.utcnow); updated_at=Column(DateTime,default=datetime.utcnow)
class AiMessage(Base):
    __tablename__="ai_messages"
    id=Column(Integer,primary_key=True); conversation_id=Column(Integer,ForeignKey("ai_conversations.id"),nullable=False,index=True); role=Column(String(20),nullable=False); content=Column(Text,nullable=False); created_at=Column(DateTime,default=datetime.utcnow)
class AiMemory(Base):
    __tablename__="ai_memories"; __table_args__=(UniqueConstraint("user_id","key",name="uq_ai_memory"),)
    id=Column(Integer,primary_key=True); user_id=Column(Integer,ForeignKey("users.id"),nullable=False,index=True); key=Column(String(80),nullable=False); value=Column(Text,nullable=False); updated_at=Column(DateTime,default=datetime.utcnow)
class MailAttachment(Base):
    __tablename__="mail_attachments"
    id=Column(Integer,primary_key=True); mail_id=Column(Integer,ForeignKey("mail.id"),nullable=False,index=True); object_key=Column(String(500),nullable=False); filename=Column(String(255),nullable=False); content_type=Column(String(120),default="application/octet-stream"); size=Column(Integer,default=0)
class Report(Base):
    __tablename__="reports"
    id=Column(Integer,primary_key=True); reporter_id=Column(Integer,ForeignKey("users.id"),nullable=False); target_type=Column(String(30),nullable=False); target_id=Column(Integer,nullable=False); reason=Column(String(300),default=""); status=Column(String(30),default="open"); created_at=Column(DateTime,default=datetime.utcnow)

app=FastAPI(title="Dura Cloud",version="4.2")

def db():
    d=SessionLocal()
    try:yield d
    finally:d.close()
def clean_address(a):
    a=a.strip().lower()
    if "@" not in a:a+="@duramail"
    if not a.endswith("@duramail"):raise HTTPException(400,"Adresse @duramail obligatoire.")
    return a
def make_token(u):return jwt.encode({"sub":str(u.id),"exp":datetime.now(timezone.utc)+timedelta(days=30)},SECRET,algorithm="HS256")
def me(authorization:str=Header(default=""),d:Session=Depends(db)):
    if not authorization.startswith("Bearer "):raise HTTPException(401,"Connexion requise.")
    try:uid=int(jwt.decode(authorization[7:],SECRET,algorithms=["HS256"])["sub"])
    except Exception:raise HTTPException(401,"Session invalide.")
    u=d.get(User,uid)
    if not u or u.is_banned:raise HTTPException(403,"Compte indisponible.")
    return u
def pub_user(u):return {"id":u.id,"address":u.address,"email":u.address,"name":u.display_name,"username":u.channel_name or u.display_name,"channel":u.channel_name or "","has_channel":bool(u.channel_name),"admin":bool(u.is_admin),"official":bool(u.is_official)}
def r2():
    if not all([R2_ENDPOINT,R2_ACCESS_KEY_ID,R2_SECRET_ACCESS_KEY,R2_BUCKET]):raise HTTPException(503,"Stockage média non configuré.")
    return boto3.client("s3",endpoint_url=R2_ENDPOINT,aws_access_key_id=R2_ACCESS_KEY_ID,aws_secret_access_key=R2_SECRET_ACCESS_KEY,region_name="auto")
def signed(key):return r2().generate_presigned_url("get_object",Params={"Bucket":R2_BUCKET,"Key":key},ExpiresIn=3600)
def pub_video(v):return {"id":v.id,"title":v.title,"description":v.description or "","owner_id":v.owner_id,"channel":v.channel,"views":v.views or 0,"likes":v.likes or 0,"short":bool(v.is_short),"featured":bool(v.is_featured),"media_url":f"/videos/{v.id}/file","thumbnail_url":f"/videos/{v.id}/thumbnail" if v.thumbnail_key else None,"created_at":v.created_at.isoformat() if v.created_at else ""}
def reject_oversize(upload:UploadFile|None,max_mb:int,label:str):
    if not upload:return
    size=getattr(upload,"size",None)
    if size is not None and size>max_mb*1024*1024:
        raise HTTPException(413,f"{label} limité à {max_mb} Mo.")


class Register(BaseModel):
    address:str; display_name:str=Field(min_length=2,max_length=80); password:str=Field(min_length=8,max_length=128); channel_name:Optional[str]=""
class Login(BaseModel):address:str;password:str
class SendMail(BaseModel):to:str;subject:str="";body:str=""
class ThemeIn(BaseModel):accent:str="#ff3158";background:str="#0f0f10";surface:str="#18181b";text:str="#f6f6f7";font:str="Segoe UI";radius:int=16;density:str="comfortable";graphic:str="clean"
class ChannelIn(BaseModel):name:str=Field(min_length=2,max_length=80);description:str=""
class VideoEdit(BaseModel):title:Optional[str]=None;description:Optional[str]=None;status:Optional[str]=None
class AiVerify(BaseModel):code:str
class AiChat(BaseModel):
    message:str=Field(min_length=1,max_length=12000)
    conversation_id:Optional[int]=None
class AiConversationIn(BaseModel):title:str="Nouvelle conversation"
class AiMemoryIn(BaseModel):key:str=Field(min_length=1,max_length=80);value:str=Field(min_length=1,max_length=4000)
class ProfileIn(BaseModel):display_name:str=Field(min_length=2,max_length=80)
class PasswordIn(BaseModel):current_password:str;new_password:str=Field(min_length=8,max_length=128)
class ReportIn(BaseModel):target_type:str;target_id:int;reason:str=""

@app.on_event("startup")
def bootstrap():
    global DATABASE_READY
    try:
        Base.metadata.create_all(engine)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        DATABASE_READY=True
    except Exception as exc:
        DATABASE_READY=False
        msg=f"Base de données indisponible au démarrage: {type(exc).__name__}: {exc}"
        CONFIG_WARNINGS.append(msg)
        logger.exception(msg)
        return
    d=SessionLocal()
    try:
        u=d.query(User).filter(User.address=="admin@duramail").first()
        if not u:
            pw=os.getenv("DURA_ADMIN_PASSWORD","").strip()
            if not pw:
                pw=secrets.token_urlsafe(18)
                CONFIG_WARNINGS.append("DURA_ADMIN_PASSWORD absent lors de la création initiale de l'admin. Un mot de passe aléatoire a été généré et écrit dans les logs Render.")
                logger.warning("MOT DE PASSE ADMIN INITIAL (à changer immédiatement): %s",pw)
            u=User(address="admin@duramail",display_name="DuraIndustry",channel_name="DuraTube",password_hash=ph.hash(pw),is_admin=True,is_official=True)
            d.add(u);d.commit();d.refresh(u)
        else:
            u.is_admin=True;u.is_official=True;u.channel_name=u.channel_name or "DuraTube";d.commit()
        if not d.query(ChannelProfile).filter_by(user_id=u.id).first():
            d.add(ChannelProfile(user_id=u.id,description="Chaîne officielle Dura."));d.commit()
    except Exception as exc:
        d.rollback()
        CONFIG_WARNINGS.append(f"Initialisation admin incomplète: {type(exc).__name__}: {exc}")
        logger.exception("Erreur bootstrap admin")
    finally:
        d.close()
    for warning in CONFIG_WARNINGS:
        logger.warning("CONFIG: %s",warning)

@app.get("/")
def status():
    return {"service":"Dura Cloud","version":"4.2","status":"online" if DATABASE_READY else "degraded",
            "release":"ui-rebuild-candidate","duratube":True,"studio":True,"duramail":True,
            "duraia":"DuraBrain Local 1.2","database":DATABASE_READY,"r2":bool(R2_ENDPOINT),
            "warnings":CONFIG_WARNINGS}

@app.get("/health")
def health():
    db_ok=False
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        db_ok=True
    except Exception:
        db_ok=False
    r2_cfg=bool(R2_ENDPOINT and R2_ACCESS_KEY_ID and R2_SECRET_ACCESS_KEY and R2_BUCKET)
    secret_persistent=bool(os.getenv("DURA_SECRET","").strip())
    return {"ok":db_ok,"database":db_ok,"r2_configured":r2_cfg,"secret_persistent":secret_persistent,
            "warnings":CONFIG_WARNINGS,"version":"4.2"}

@app.get("/ready")
def ready():
    if not DATABASE_READY:
        raise HTTPException(503,"Dura Cloud démarre mais la base de données n'est pas prête. Consulte /health et les logs Render.")
    return {"ready":True,"version":"4.2"}

@app.patch("/account/profile")
def update_profile(x:ProfileIn,u:User=Depends(me),d:Session=Depends(db)):
    u.display_name=x.display_name.strip();d.commit();return pub_user(u)

@app.post("/auth/change-password")
def change_password(x:PasswordIn,u:User=Depends(me),d:Session=Depends(db)):
    try:ok=ph.verify(u.password_hash,x.current_password)
    except VerifyMismatchError:ok=False
    if not ok:raise HTTPException(400,"Mot de passe actuel incorrect.")
    u.password_hash=ph.hash(x.new_password);d.commit();return {"ok":True}

@app.post("/auth/register")
def register(x:Register,d:Session=Depends(db)):
    a=clean_address(x.address)
    if d.query(User).filter(User.address==a).first():raise HTTPException(409,"Cette adresse @duramail existe déjà.")
    ch=(x.channel_name or "").strip()
    if ch and d.query(User).filter(User.channel_name.ilike(ch)).first():raise HTTPException(409,"Ce nom de chaîne existe déjà.")
    u=User(address=a,display_name=x.display_name.strip(),channel_name=ch,password_hash=ph.hash(x.password));d.add(u);d.commit();d.refresh(u)
    d.add(ThemeProfile(user_id=u.id));
    if ch:d.add(ChannelProfile(user_id=u.id))
    official=d.query(User).filter(User.is_official==True).first()
    if official and official.id!=u.id:d.add(Subscription(subscriber_id=u.id,channel_id=official.id))
    d.commit();return {"token":make_token(u),"user":pub_user(u)}
@app.post("/auth/login")
def login(x:Login,d:Session=Depends(db)):
    u=d.query(User).filter(User.address==clean_address(x.address)).first()
    if not u:raise HTTPException(401,"Adresse ou mot de passe incorrect.")
    try:ok=ph.verify(u.password_hash,x.password)
    except VerifyMismatchError:ok=False
    if not ok:raise HTTPException(401,"Adresse ou mot de passe incorrect.")
    if u.is_banned:raise HTTPException(403,"Compte suspendu.")
    return {"token":make_token(u),"user":pub_user(u)}
@app.get("/auth/me")
def auth_me(u:User=Depends(me)):return pub_user(u)

@app.get("/theme/me")
def theme_me(u:User=Depends(me),d:Session=Depends(db)):
    t=d.query(ThemeProfile).filter_by(user_id=u.id).first()
    if not t:t=ThemeProfile(user_id=u.id);d.add(t);d.commit();d.refresh(t)
    return {k:getattr(t,k) for k in ["accent","background","surface","text","font","radius","density","graphic"]}
@app.put("/theme/me")
def theme_save(x:ThemeIn,u:User=Depends(me),d:Session=Depends(db)):
    t=d.query(ThemeProfile).filter_by(user_id=u.id).first() or ThemeProfile(user_id=u.id)
    for k,v in x.model_dump().items():setattr(t,k,v)
    d.add(t);d.commit();return {"ok":True}

@app.post("/channel/create")
def create_channel(x:ChannelIn,u:User=Depends(me),d:Session=Depends(db)):
    if u.channel_name:raise HTTPException(409,"Tu as déjà une chaîne.")
    if d.query(User).filter(User.channel_name.ilike(x.name.strip())).first():raise HTTPException(409,"Nom de chaîne déjà utilisé.")
    u.channel_name=x.name.strip();p=ChannelProfile(user_id=u.id,description=x.description[:2000]);d.add(p);d.commit();return pub_user(u)
@app.get("/channel/me")
def channel_me(u:User=Depends(me),d:Session=Depends(db)):
    if not u.channel_name:raise HTTPException(404,"Aucune chaîne.")
    p=d.query(ChannelProfile).filter_by(user_id=u.id).first() or ChannelProfile(user_id=u.id)
    if not p.id:d.add(p);d.commit();d.refresh(p)
    return pub_user(u)|{"description":p.description or "","accent":p.accent,"font":p.font,"layout":p.layout,"logo_url":f"/channel/{u.id}/logo" if p.logo_key else None,"banner_url":f"/channel/{u.id}/banner" if p.banner_key else None,"subscribers":d.query(Subscription).filter_by(channel_id=u.id).count(),"videos":d.query(Video).filter_by(owner_id=u.id,status="published").count()}
@app.get("/channel/{uid}")
def channel(uid:int,d:Session=Depends(db)):
    u=d.get(User,uid)
    if not u or not u.channel_name:raise HTTPException(404)
    p=d.query(ChannelProfile).filter_by(user_id=uid).first() or ChannelProfile(user_id=uid)
    return pub_user(u)|{"description":p.description or "","accent":p.accent,"font":p.font,"layout":p.layout,"logo_url":f"/channel/{uid}/logo" if p.logo_key else None,"banner_url":f"/channel/{uid}/banner" if p.banner_key else None,"subscribers":d.query(Subscription).filter_by(channel_id=uid).count(),"videos":d.query(Video).filter_by(owner_id=uid,status="published").count()}
@app.post("/channel/assets")
async def channel_assets(description:str=Form(""),accent:str=Form("#7c5cff"),font:str=Form("Segoe UI"),layout:str=Form("modern"),logo:UploadFile|None=File(None),banner:UploadFile|None=File(None),u:User=Depends(me),d:Session=Depends(db)):
    if not u.channel_name:raise HTTPException(403,"Crée une chaîne d'abord.")
    p=d.query(ChannelProfile).filter_by(user_id=u.id).first() or ChannelProfile(user_id=u.id)
    p.description=description[:2000];p.accent=accent[:20];p.font=font[:80];p.layout=layout[:30]
    for f,attr,prefix in [(logo,"logo_key","logos"),(banner,"banner_key","banners")]:
        if f and f.filename:
            ext=Path(f.filename).suffix.lower()
            if ext not in {".jpg",".jpeg",".png",".webp"}:raise HTTPException(400,"Image non acceptée.")
            key=f"channels/{u.id}/{prefix}/{uuid.uuid4().hex}{ext}";r2().upload_fileobj(f.file,R2_BUCKET,key,ExtraArgs={"ContentType":f.content_type or "image/jpeg"});setattr(p,attr,key)
    d.add(p);d.commit();return {"ok":True}
@app.get("/channel/{uid}/logo")
def channel_logo(uid:int,d:Session=Depends(db)):
    p=d.query(ChannelProfile).filter_by(user_id=uid).first()
    if not p or not p.logo_key:raise HTTPException(404)
    return RedirectResponse(signed(p.logo_key))
@app.get("/channel/{uid}/banner")
def channel_banner(uid:int,d:Session=Depends(db)):
    p=d.query(ChannelProfile).filter_by(user_id=uid).first()
    if not p or not p.banner_key:raise HTTPException(404)
    return RedirectResponse(signed(p.banner_key))

@app.post("/mail")
def send_mail(x:SendMail,u:User=Depends(me),d:Session=Depends(db)):
    r=d.query(User).filter(User.address==clean_address(x.to)).first()
    if not r:raise HTTPException(404,"Adresse DuraMail inexistante.")
    d.add(Mail(sender_id=u.id,recipient_id=r.id,subject=x.subject[:180],body=x.body[:100000]));d.commit();return {"ok":True}
@app.get("/mail")
def get_mail(folder:str="inbox",u:User=Depends(me),d:Session=Depends(db)):
    if folder=="sent":rows=d.query(Mail).filter(Mail.sender_id==u.id,Mail.trash_sender==False).order_by(Mail.id.desc()).all()
    elif folder=="trash":rows=d.query(Mail).filter(or_((Mail.sender_id==u.id)&(Mail.trash_sender==True),(Mail.recipient_id==u.id)&(Mail.trash_recipient==True))).order_by(Mail.id.desc()).all()
    else:rows=d.query(Mail).filter(Mail.recipient_id==u.id,Mail.trash_recipient==False).order_by(Mail.id.desc()).all()
    out=[]
    for m in rows:
        s=d.get(User,m.sender_id);r=d.get(User,m.recipient_id)
        out.append({"id":m.id,"from":s.address,"to":r.address,"peer":r.address if m.sender_id==u.id else s.address,"subject":m.subject,"body":m.body,"read":bool(m.is_read),"starred":bool(m.is_starred),"date":m.created_at.strftime("%d/%m/%Y %H:%M")})
    return out
@app.post("/mail/{mid}/read")
def read_mail(mid:int,u:User=Depends(me),d:Session=Depends(db)):
    m=d.get(Mail,mid)
    if not m or m.recipient_id!=u.id:raise HTTPException(404)
    m.is_read=True;d.commit();return {"ok":True}
@app.post("/mail/{mid}/star")
def star_mail(mid:int,u:User=Depends(me),d:Session=Depends(db)):
    m=d.get(Mail,mid)
    if not m or u.id not in (m.sender_id,m.recipient_id):raise HTTPException(404)
    m.is_starred=not m.is_starred;d.commit();return {"starred":m.is_starred}
@app.delete("/mail/{mid}")
def delete_mail(mid:int,u:User=Depends(me),d:Session=Depends(db)):
    m=d.get(Mail,mid)
    if not m:raise HTTPException(404)
    if m.sender_id==u.id:m.trash_sender=True
    if m.recipient_id==u.id:m.trash_recipient=True
    d.commit();return {"ok":True}

@app.post("/mail/send")
async def send_mail_with_attachment(to:str=Form(...),subject:str=Form(""),body:str=Form(""),attachment:UploadFile|None=File(None),u:User=Depends(me),d:Session=Depends(db)):
    recipient=d.query(User).filter(User.address==clean_address(to)).first()
    if not recipient:raise HTTPException(404,"Adresse DuraMail inexistante.")
    reject_oversize(attachment,MAX_MAIL_ATTACHMENT_MB,"Pièce jointe")
    object_key=None
    attachment_size=0
    if attachment and attachment.filename:
        if getattr(attachment,"size",None) is None:
            raw=attachment.file.read(MAX_MAIL_ATTACHMENT_MB*1024*1024+1);attachment.file.seek(0)
            if len(raw)>MAX_MAIL_ATTACHMENT_MB*1024*1024:raise HTTPException(413,f"Pièce jointe limitée à {MAX_MAIL_ATTACHMENT_MB} Mo.")
            attachment_size=len(raw)
        else:
            attachment_size=int(attachment.size)
        ext=Path(attachment.filename).suffix.lower()
        object_key=f"mail/{u.id}/{uuid.uuid4().hex}{ext}"
        try:
            r2().upload_fileobj(attachment.file,R2_BUCKET,object_key,ExtraArgs={"ContentType":attachment.content_type or "application/octet-stream"})
        except Exception as exc:
            logger.exception("Upload pièce jointe impossible")
            raise HTTPException(503,"Le stockage des pièces jointes est temporairement indisponible.") from exc
    try:
        m=Mail(sender_id=u.id,recipient_id=recipient.id,subject=subject[:180],body=body[:100000]);d.add(m);d.flush()
        if object_key:
            d.add(MailAttachment(mail_id=m.id,object_key=object_key,filename=attachment.filename[:255],content_type=attachment.content_type or "application/octet-stream",size=attachment_size))
        d.commit();d.refresh(m)
        return {"ok":True,"id":m.id}
    except Exception:
        d.rollback()
        if object_key:
            try:r2().delete_object(Bucket=R2_BUCKET,Key=object_key)
            except Exception:pass
        raise

@app.get("/mail/{mid}/attachments")
def mail_attachments(mid:int,u:User=Depends(me),d:Session=Depends(db)):
    m=d.get(Mail,mid)
    if not m or u.id not in (m.sender_id,m.recipient_id):raise HTTPException(404)
    return [{"id":a.id,"filename":a.filename,"content_type":a.content_type,"size":a.size,"url":f"/mail/attachment/{a.id}"} for a in d.query(MailAttachment).filter_by(mail_id=mid).all()]

@app.get("/mail/attachment/{aid}")
def mail_attachment(aid:int,u:User=Depends(me),d:Session=Depends(db)):
    a=d.get(MailAttachment,aid)
    if not a:raise HTTPException(404)
    m=d.get(Mail,a.mail_id)
    if not m or u.id not in (m.sender_id,m.recipient_id):raise HTTPException(403)
    return RedirectResponse(signed(a.object_key))

@app.get("/mail/unread-count")
def unread_count(u:User=Depends(me),d:Session=Depends(db)):
    return {"count":d.query(Mail).filter(Mail.recipient_id==u.id,Mail.is_read==False,Mail.trash_recipient==False).count()}

@app.post("/videos/upload")
async def upload_video(title:str=Form(...),description:str=Form(""),is_short:bool=Form(False),file:UploadFile=File(...),thumbnail:UploadFile|None=File(None),u:User=Depends(me),d:Session=Depends(db)):
    if not u.channel_name:raise HTTPException(403,"Crée une chaîne avant de publier.")
    reject_oversize(file,MAX_VIDEO_MB,"Vidéo");reject_oversize(thumbnail,20,"Miniature")
    ext=Path(file.filename or "").suffix.lower()
    if ext not in {".mp4",".mov",".mkv",".avi",".webm",".m4v"}:raise HTTPException(400,"Format vidéo non accepté.")
    key=f"videos/{u.id}/{uuid.uuid4().hex}{ext}"
    try:r2().upload_fileobj(file.file,R2_BUCKET,key,ExtraArgs={"ContentType":file.content_type or "video/mp4"})
    except Exception as exc:
        logger.exception("Upload vidéo R2 impossible");raise HTTPException(503,"Stockage vidéo temporairement indisponible.") from exc
    tk=None
    if thumbnail and thumbnail.filename:
        te=Path(thumbnail.filename).suffix.lower()
        if te not in {".jpg",".jpeg",".png",".webp"}:raise HTTPException(400,"Miniature non acceptée.")
        tk=f"thumbnails/{u.id}/{uuid.uuid4().hex}{te}";r2().upload_fileobj(thumbnail.file,R2_BUCKET,tk,ExtraArgs={"ContentType":thumbnail.content_type or "image/jpeg"})
    v=Video(owner_id=u.id,title=title.strip()[:180] or "Sans titre",description=description[:10000],channel=u.channel_name,object_key=key,thumbnail_key=tk,is_short=is_short,is_featured=bool(u.is_official));d.add(v);d.commit();d.refresh(v);return pub_video(v)
@app.get("/videos")
def videos(q:str="",short:int=-1,owner_id:int=0,d:Session=Depends(db)):
    x=d.query(Video).filter(Video.status=="published")
    if q:x=x.filter(Video.title.ilike(f"%{q}%"))
    if short in (0,1):x=x.filter(Video.is_short==bool(short))
    if owner_id:x=x.filter(Video.owner_id==owner_id)
    rows=x.all();rows.sort(key=lambda v:(bool(v.is_featured),v.id),reverse=True);return [pub_video(v) for v in rows]
@app.get("/feed")
def feed(d:Session=Depends(db)):
    rows=d.query(Video).filter(Video.status=="published",Video.is_short==False).all();active={p.video_id:p for p in d.query(Promotion).filter(Promotion.status=="active").all() if p.delivered_impressions<p.target_impressions}
    rows.sort(key=lambda v:((1000000 if v.is_featured else 0)+(500000 if v.id in active else 0)+(v.views or 0)+(v.likes or 0)*5+v.id),reverse=True)
    for v in rows[:30]:
        p=active.get(v.id)
        if p:p.delivered_impressions+=1;p.status="completed" if p.delivered_impressions>=p.target_impressions else "active"
    d.commit();return [pub_video(v)|{"promoted":v.id in active} for v in rows[:30]]
@app.get("/videos/{vid}")
def video_details(vid:int,d:Session=Depends(db)):
    v=d.get(Video,vid)
    if not v or v.status!="published":raise HTTPException(404)
    owner=d.get(User,v.owner_id)
    subs=d.query(Subscription).filter_by(channel_id=v.owner_id).count()
    return pub_video(v)|{"subscribers":subs,"owner":pub_user(owner) if owner else None}

@app.get("/videos/{vid}/file")
def video_file(vid:int,d:Session=Depends(db)):
    v=d.get(Video,vid)
    if not v:raise HTTPException(404)
    return RedirectResponse(signed(v.object_key))
@app.get("/videos/{vid}/thumbnail")
def video_thumb(vid:int,d:Session=Depends(db)):
    v=d.get(Video,vid)
    if not v or not v.thumbnail_key:raise HTTPException(404)
    return RedirectResponse(signed(v.thumbnail_key))
@app.post("/videos/{vid}/view")
def view(vid:int,d:Session=Depends(db)):
    v=d.get(Video,vid)
    if not v:raise HTTPException(404)
    v.views=(v.views or 0)+1;d.commit();return {"views":v.views}
@app.post("/videos/{vid}/like")
def like(vid:int,u:User=Depends(me),d:Session=Depends(db)):
    v=d.get(Video,vid)
    if not v:raise HTTPException(404)
    old=d.query(VideoLike).filter_by(video_id=vid,user_id=u.id).first()
    if old:d.delete(old);v.likes=max(0,(v.likes or 0)-1);liked=False
    else:d.add(VideoLike(video_id=vid,user_id=u.id));v.likes=(v.likes or 0)+1;liked=True
    d.commit();return {"liked":liked,"likes":v.likes}
@app.patch("/videos/{vid}")
def edit_video(vid:int,x:VideoEdit,u:User=Depends(me),d:Session=Depends(db)):
    v=d.get(Video,vid)
    if not v or (v.owner_id!=u.id and not u.is_admin):raise HTTPException(404)
    for k,val in x.model_dump(exclude_none=True).items():
        if k=="title":val=val[:180]
        if k=="description":val=val[:10000]
        if k=="status" and val not in {"published","private","unlisted"}:raise HTTPException(400)
        setattr(v,k,val)
    d.commit();return pub_video(v)
@app.delete("/videos/{vid}")
def delete_video(vid:int,u:User=Depends(me),d:Session=Depends(db)):
    v=d.get(Video,vid)
    if not v or (v.owner_id!=u.id and not u.is_admin):raise HTTPException(404)
    try:r2().delete_object(Bucket=R2_BUCKET,Key=v.object_key)
    except Exception:pass
    if v.thumbnail_key:
        try:r2().delete_object(Bucket=R2_BUCKET,Key=v.thumbnail_key)
        except Exception:pass
    d.delete(v);d.commit();return {"ok":True}
@app.get("/videos/{vid}/comments")
def comments(vid:int,d:Session=Depends(db)):
    out=[]
    for c in d.query(Comment).filter_by(video_id=vid).order_by(Comment.id.desc()).all():
        author=d.get(User,c.user_id)
        out.append({"id":c.id,"user_id":c.user_id,"username":(author.channel_name or author.display_name) if author else "Utilisateur","official":bool(author and author.is_official),"content":c.content,"created_at":c.created_at.isoformat()})
    return out
@app.post("/videos/{vid}/comments")
def add_comment(vid:int,content:str=Form(...),u:User=Depends(me),d:Session=Depends(db)):
    if not d.get(Video,vid):raise HTTPException(404)
    c=Comment(video_id=vid,user_id=u.id,content=content.strip()[:1000])
    if not c.content:raise HTTPException(400)
    d.add(c);d.commit();return {"ok":True}

@app.get("/videos/{vid}/related")
def related_videos(vid:int,d:Session=Depends(db)):
    current=d.get(Video,vid)
    if not current:raise HTTPException(404)
    rows=d.query(Video).filter(Video.status=="published",Video.is_short==False,Video.id!=vid).all()
    rows.sort(key=lambda v:((v.owner_id==current.owner_id),(v.likes or 0),(v.views or 0),v.id),reverse=True)
    return [pub_video(v) for v in rows[:12]]

@app.get("/channels")
def channels(q:str="",d:Session=Depends(db)):
    x=d.query(User).filter(User.is_banned==False,User.channel_name!="")
    if q:x=x.filter(User.channel_name.ilike(f"%{q}%"))
    return [pub_user(u)|{"subscribers":d.query(Subscription).filter_by(channel_id=u.id).count(),"videos":d.query(Video).filter_by(owner_id=u.id,status="published").count(),"logo_url":f"/channel/{u.id}/logo" if (d.query(ChannelProfile).filter_by(user_id=u.id).first() and d.query(ChannelProfile).filter_by(user_id=u.id).first().logo_key) else None} for u in x.all()]
@app.post("/subscriptions/{channel_id}")
def subscribe(channel_id:int,u:User=Depends(me),d:Session=Depends(db)):
    if channel_id==u.id:raise HTTPException(400)
    if not d.get(User,channel_id):raise HTTPException(404)
    if not d.query(Subscription).filter_by(subscriber_id=u.id,channel_id=channel_id).first():d.add(Subscription(subscriber_id=u.id,channel_id=channel_id));d.commit()
    return {"subscribed":True}
@app.delete("/subscriptions/{channel_id}")
def unsubscribe(channel_id:int,u:User=Depends(me),d:Session=Depends(db)):
    s=d.query(Subscription).filter_by(subscriber_id=u.id,channel_id=channel_id).first()
    if s:d.delete(s);d.commit()
    return {"subscribed":False}
@app.get("/subscriptions/status/{channel_id}")
def subscription_status(channel_id:int,u:User=Depends(me),d:Session=Depends(db)):
    return {"subscribed":d.query(Subscription).filter_by(subscriber_id=u.id,channel_id=channel_id).first() is not None,"subscribers":d.query(Subscription).filter_by(channel_id=channel_id).count()}

@app.get("/subscriptions/me")
def my_subscriptions(u:User=Depends(me),d:Session=Depends(db)):
    rows=d.query(Subscription).filter_by(subscriber_id=u.id).all();out=[]
    for row in rows:
        ch=d.get(User,row.channel_id)
        if ch:out.append(pub_user(ch)|{"subscribers":d.query(Subscription).filter_by(channel_id=ch.id).count(),"videos":d.query(Video).filter_by(owner_id=ch.id,status="published").count()})
    return out

@app.get("/studio/dashboard")
def studio_dashboard(u:User=Depends(me),d:Session=Depends(db)):
    if not u.channel_name:raise HTTPException(403,"Aucune chaîne.")
    vids=d.query(Video).filter_by(owner_id=u.id).order_by(Video.id.desc()).all();subs=d.query(Subscription).filter_by(channel_id=u.id).count()
    return {"channel":u.channel_name,"subscribers":subs,"videos":sum(1 for v in vids if not v.is_short),"shorts":sum(1 for v in vids if v.is_short),"uploads":len(vids),"views":sum(v.views or 0 for v in vids),"likes":sum(v.likes or 0 for v in vids),"recent":[pub_video(v) for v in vids[:8]]}
@app.get("/studio/content")
def studio_content(u:User=Depends(me),d:Session=Depends(db)):
    if not u.channel_name:raise HTTPException(403,"Aucune chaîne.")
    return [pub_video(v)|{"status":v.status} for v in d.query(Video).filter_by(owner_id=u.id).order_by(Video.id.desc()).all()]

@app.get("/studio/analytics")
def studio_analytics(u:User=Depends(me),d:Session=Depends(db)):
    if not u.channel_name:raise HTTPException(403,"Aucune chaîne.")
    vids=d.query(Video).filter_by(owner_id=u.id).all();total_views=sum(v.views or 0 for v in vids);total_likes=sum(v.likes or 0 for v in vids)
    top=sorted(vids,key=lambda v:(v.views or 0,v.likes or 0),reverse=True)[:5]
    return {"subscribers":d.query(Subscription).filter_by(channel_id=u.id).count(),"videos":len(vids),"views":total_views,"likes":total_likes,"engagement":round((total_likes/max(total_views,1))*100,2),"top":[pub_video(v) for v in top]}

@app.post("/reports")
def create_report(x:ReportIn,u:User=Depends(me),d:Session=Depends(db)):
    if x.target_type not in {"video","post","comment","channel"}:raise HTTPException(400,"Type de signalement invalide.")
    r=Report(reporter_id=u.id,target_type=x.target_type,target_id=x.target_id,reason=x.reason[:300]);d.add(r);d.commit();return {"ok":True,"id":r.id}

@app.get("/admin/stats")
def admin_stats(u:User=Depends(me),d:Session=Depends(db)):
    if not u.is_admin:raise HTTPException(403)
    return {"users":d.query(User).count(),"videos":d.query(Video).count(),"posts":d.query(Post).count(),"mails":d.query(Mail).count(),"reports_open":d.query(Report).filter_by(status="open").count()}

@app.get("/admin/reports")
def admin_reports(u:User=Depends(me),d:Session=Depends(db)):
    if not u.is_admin:raise HTTPException(403)
    return [{"id":r.id,"target_type":r.target_type,"target_id":r.target_id,"reason":r.reason,"status":r.status,"created_at":r.created_at.isoformat()} for r in d.query(Report).order_by(Report.id.desc()).limit(200).all()]

@app.get("/admin/users")
def admin_users(u:User=Depends(me),d:Session=Depends(db)):
    if not u.is_admin:raise HTTPException(403)
    return [pub_user(x)|{"banned":bool(x.is_banned),"created_at":x.created_at.isoformat() if x.created_at else ""} for x in d.query(User).order_by(User.id.desc()).limit(500).all()]

@app.post("/admin/users/{uid}/ban")
def admin_ban(uid:int,u:User=Depends(me),d:Session=Depends(db)):
    if not u.is_admin:raise HTTPException(403)
    x=d.get(User,uid)
    if not x or x.is_official:raise HTTPException(400,"Action impossible.")
    x.is_banned=not x.is_banned;d.commit();return {"banned":x.is_banned}

@app.post("/admin/reports/{rid}/resolve")
def admin_resolve_report(rid:int,u:User=Depends(me),d:Session=Depends(db)):
    if not u.is_admin:raise HTTPException(403)
    r=d.get(Report,rid)
    if not r:raise HTTPException(404)
    r.status="resolved";d.commit();return {"ok":True}

@app.post("/admin/videos/{vid}/hide")
def admin_hide_video(vid:int,u:User=Depends(me),d:Session=Depends(db)):
    if not u.is_admin:raise HTTPException(403)
    v=d.get(Video,vid)
    if not v:raise HTTPException(404)
    v.status="private" if v.status=="published" else "published";d.commit();return {"status":v.status}

@app.get("/posts")
def posts(d:Session=Depends(db)):
    out=[]
    for p in d.query(Post).order_by(Post.id.desc()).all():
        usr=d.get(User,p.user_id);out.append({"id":p.id,"user_id":p.user_id,"username":usr.channel_name or usr.display_name,"content":p.content or "","media_type":p.media_type,"media_url":f"/posts/{p.id}/media" if p.media_key else None,"likes":p.likes or 0,"created_at":p.created_at.isoformat()})
    return out
@app.post("/posts")
async def create_post(content:str=Form(""),media:UploadFile|None=File(None),u:User=Depends(me),d:Session=Depends(db)):
    key=None;typ="none"
    if media and media.filename:
        reject_oversize(media,MAX_POST_MEDIA_MB,"Média")
        ext=Path(media.filename).suffix.lower();typ="image" if ext in {".jpg",".jpeg",".png",".webp",".gif"} else "video" if ext in {".mp4",".mov",".webm",".mkv"} else ""
        if not typ:raise HTTPException(400,"Média non accepté.")
        key=f"posts/{u.id}/{uuid.uuid4().hex}{ext}";r2().upload_fileobj(media.file,R2_BUCKET,key,ExtraArgs={"ContentType":media.content_type or "application/octet-stream"})
    if not content.strip() and not key:raise HTTPException(400,"Post vide.")
    p=Post(user_id=u.id,content=content.strip()[:3000],media_key=key,media_type=typ);d.add(p);d.commit();d.refresh(p);return {"id":p.id}
@app.get("/posts/{pid}/media")
def post_media(pid:int,d:Session=Depends(db)):
    p=d.get(Post,pid)
    if not p or not p.media_key:raise HTTPException(404)
    return RedirectResponse(signed(p.media_key))
@app.post("/posts/{pid}/like")
def post_like(pid:int,u:User=Depends(me),d:Session=Depends(db)):
    p=d.get(Post,pid)
    if not p:raise HTTPException(404)
    old=d.query(PostLike).filter_by(post_id=pid,user_id=u.id).first()
    if old:d.delete(old);p.likes=max(0,(p.likes or 0)-1);liked=False
    else:d.add(PostLike(post_id=pid,user_id=u.id));p.likes=(p.likes or 0)+1;liked=True
    d.commit();return {"liked":liked,"likes":p.likes}

# ---------------- DURAIA / DURABRAIN LOCAL ----------------
STOPWORDS={"le","la","les","un","une","des","de","du","et","ou","a","à","au","aux","en","dans","pour","par","sur","avec","sans","ce","cette","ces","je","tu","il","elle","on","nous","vous","ils","elles","que","qui","quoi","est","sont","être","avoir","fait","faire","plus","pas","ne","mon","ma","mes","ton","ta","tes","son","sa","ses"}

class DuraBrain:
    def tokenize(self,text):return re.findall(r"[a-zA-ZÀ-ÿ0-9']+",text.lower())
    def sentences(self,text):return [x.strip() for x in re.split(r"(?<=[.!?])\s+|\n+",text.strip()) if x.strip()]
    def safe_math(self,expr):
        allowed={ast.Add:lambda a,b:a+b,ast.Sub:lambda a,b:a-b,ast.Mult:lambda a,b:a*b,ast.Div:lambda a,b:a/b,ast.FloorDiv:lambda a,b:a//b,ast.Mod:lambda a,b:a%b,ast.Pow:lambda a,b:a**b,ast.USub:lambda a:-a,ast.UAdd:lambda a:+a}
        def ev(n):
            if isinstance(n,ast.Expression):return ev(n.body)
            if isinstance(n,ast.Constant) and isinstance(n.value,(int,float)):return n.value
            if isinstance(n,ast.BinOp) and type(n.op) in allowed:
                a,b=ev(n.left),ev(n.right)
                if isinstance(n.op,ast.Pow) and abs(b)>10:raise ValueError()
                return allowed[type(n.op)](a,b)
            if isinstance(n,ast.UnaryOp) and type(n.op) in allowed:return allowed[type(n.op)](ev(n.operand))
            raise ValueError()
        tree=ast.parse(expr,mode="eval");return ev(tree)
    def summarize(self,text,limit=4):
        sents=self.sentences(text)
        if len(sents)<=limit:return "\n".join(sents)
        words=[w for w in self.tokenize(text) if w not in STOPWORDS and len(w)>2];freq=Counter(words)
        scored=[]
        for i,s in enumerate(sents):
            toks=[w for w in self.tokenize(s) if w not in STOPWORDS];score=sum(freq[w] for w in toks)/(len(toks)+1);scored.append((score,i,s))
        chosen=sorted(sorted(scored,reverse=True)[:limit],key=lambda x:x[1]);return " ".join(x[2] for x in chosen)
    def rewrite(self,text):
        t=text.strip();t=re.sub(r"\s+"," ",t);t=re.sub(r"\s+([,.!?;:])",r"\1",t)
        if t:t=t[0].upper()+t[1:]
        if t and t[-1] not in ".!?":t+="."
        return t
    def brainstorm(self,topic):
        topic=topic.strip() or "ton projet"
        frames=["Version simple et rapide","Version premium","Angle communauté","Angle viral","Angle utile au quotidien","Angle automatisation","Angle personnalisation","Angle collaboration"]
        return "\n".join(f"{i+1}. {f} autour de {topic}." for i,f in enumerate(frames))
    def plan(self,goal):
        return f"Objectif : {goal.strip()}\n\n1. Définir le résultat exact.\n2. Faire une version minimale testable.\n3. Tester avec un vrai utilisateur.\n4. Corriger les blocages.\n5. Ajouter les fonctions importantes.\n6. Vérifier sécurité, erreurs et sauvegardes.\n7. Préparer la publication et une checklist de lancement."
    def answer(self,message,memories,context=None):
        raw=message.strip();low=raw.lower();tokens=set(self.tokenize(raw));context=context or []
        if low.startswith(("calcule ","calcul ")):
            expr=raw.split(" ",1)[1].replace("×","*").replace("÷","/").replace("^","**")
            try:return f"Résultat : {self.safe_math(expr)}"
            except Exception:return "Je n'arrive pas à interpréter ce calcul. Utilise par exemple : calcule (12+8)*3."
        if re.fullmatch(r"[0-9\s+\-*/().,%^]+",raw):
            try:return f"Résultat : {self.safe_math(raw.replace('^','**'))}"
            except Exception:pass
        if low.startswith(("résume ","resume ","résumer ","resumer ")):
            text=raw.split(" ",1)[1] if " " in raw else "";return "Résumé :\n"+self.summarize(text)
        if low.startswith(("réécris ","reecris ","corrige ","reformule ")):
            text=raw.split(" ",1)[1] if " " in raw else "";return self.rewrite(text)
        if low.startswith(("idées ","idees ","brainstorm ")):
            topic=raw.split(" ",1)[1] if " " in raw else ""; topic=re.sub(r"^(pour|sur)\s+","",topic,flags=re.I); return self.brainstorm(topic)
        if low.startswith(("plan ","planifie ","organise ")):
            return self.plan(raw.split(" ",1)[1] if " " in raw else raw)
        if low.startswith(("explique ","explique-moi ")):
            subject=raw.split(" ",1)[1] if " " in raw else raw
            return f"Explication structurée de {subject} :\n\n• Idée principale : identifie ce que c'est et à quoi ça sert.\n• Fonctionnement : découpe le sujet en étapes simples.\n• Exemple : applique-le à un cas concret.\n• Vérification : regarde ce qui peut échouer ou être mal compris.\n\nSi tu me donnes le texte ou les données exactes, je peux les restructurer directement."
        if low.startswith(("liste ","fais une liste ")):
            subject=raw.split(" ",1)[1] if " " in raw else raw
            return "Liste de travail :\n"+"\n".join(f"{i}. {x}" for i,x in enumerate([f"Définir {subject}",f"Préparer les éléments nécessaires",f"Construire une première version",f"Tester le résultat",f"Corriger les problèmes",f"Finaliser et publier"],1))
        if low.startswith(("compare ","comparaison ")):
            subject=raw.split(" ",1)[1] if " " in raw else raw
            return f"Comparaison de {subject} :\n\n1. Objectif\n2. Facilité d'utilisation\n3. Fonctionnalités\n4. Performances\n5. Personnalisation\n6. Coût et contraintes\n7. Meilleur choix selon l'usage"
        if low.startswith(("titre ","titres ")):
            topic=raw.split(" ",1)[1] if " " in raw else "ton sujet"
            return "Propositions de titres :\n"+"\n".join([f"• {topic} : le guide complet",f"• Tout comprendre sur {topic}",f"• {topic} : ce qu'il faut savoir",f"• J'ai testé {topic}",f"• {topic}, mais en mieux"])
        if "duratube" in tokens:
            return "DuraTube est la plateforme vidéo de l'écosystème Dura. Le compte est un compte Dura @duramail, les médias sont stockés sur R2 et DuraTube Studio sert à gérer une chaîne."
        if "duramail" in tokens:
            return "DuraMail est la messagerie interne de l'écosystème Dura. Les messages passent par Dura Cloud et peuvent contenir une pièce jointe."
        if "studio" in tokens and "dura" in low:
            return "DuraTube Studio sert à gérer ta chaîne : vidéos, statistiques, personnalisation et communauté."
        if any(x in low for x in ["qui suis-je","que sais-tu sur moi","tu sais quoi sur moi"]):
            if not memories:return "Je n'ai encore rien mémorisé à ton sujet dans DuraIA."
            return "Voici ce que j'ai en mémoire :\n"+"\n".join(f"• {k} : {v}" for k,v in memories.items())
        if low in {"bonjour","salut","hello","hey","wesh"}:return "Salut. Je suis DuraIA, le moteur IA maison de l'écosystème Dura. Je peux calculer, résumer, reformuler, brainstormer, planifier et t'aider sur les apps Dura."
        if "merci" in tokens:return "Avec plaisir."
        if any(x in low for x in ["et après","et apres","continue","suite"]) and context:
            previous=next((m.get("content","") for m in reversed(context) if m.get("role")=="user"),"")
            if previous:return self.plan(previous)
        # Lightweight keyword synthesis, no external provider.
        important=[w for w in self.tokenize(raw) if w not in STOPWORDS and len(w)>2][:8]
        subject=" ".join(important[:4]) or "ta demande"
        return f"Je comprends que ta demande concerne {subject}. Mon moteur local n'est pas un grand modèle de langage externe : je peux surtout structurer le problème.\n\n{self.plan(raw)}"

BRAIN=DuraBrain()

def ai_verified(u,d):
    a=d.query(AiAccess).filter_by(user_id=u.id).first()
    if not a or not a.verified:raise HTTPException(403,"Vérifie d'abord ton accès DuraIA.")

def ai_memories(user_id,d):return {m.key:m.value for m in d.query(AiMemory).filter_by(user_id=user_id).all()}

@app.get("/ai/status")
def ai_status(u:User=Depends(me),d:Session=Depends(db)):
    a=d.query(AiAccess).filter_by(user_id=u.id).first();return {"verified":bool(a and a.verified),"engine":"DuraBrain Local 1.2","provider_ready":True,"external_api":False}

@app.post("/ai/request-code")
def ai_request_code(u:User=Depends(me),d:Session=Depends(db)):
    code=f"{random.randint(0,999999):06d}";h=hashlib.sha256(code.encode()).hexdigest();d.add(AiCode(user_id=u.id,code_hash=h,expires_at=datetime.utcnow()+timedelta(minutes=10)))
    admin=d.query(User).filter(User.is_official==True).first()
    if not admin:raise HTTPException(500,"Compte système absent.")
    d.add(Mail(sender_id=admin.id,recipient_id=u.id,subject="Code d'accès DuraIA",body=f"Ton code DuraIA est : {code}\nIl expire dans 10 minutes."));d.commit();return {"ok":True,"message":"Code envoyé dans DuraMail."}

@app.post("/ai/verify")
def ai_verify(x:AiVerify,u:User=Depends(me),d:Session=Depends(db)):
    h=hashlib.sha256(x.code.strip().encode()).hexdigest();row=d.query(AiCode).filter_by(user_id=u.id,used=False).order_by(AiCode.id.desc()).first()
    if not row or row.expires_at<datetime.utcnow() or row.code_hash!=h:raise HTTPException(400,"Code invalide ou expiré.")
    row.used=True;a=d.query(AiAccess).filter_by(user_id=u.id).first() or AiAccess(user_id=u.id);a.verified=True;a.verified_at=datetime.utcnow();d.add(a);d.commit();return {"verified":True}

@app.get("/ai/conversations")
def ai_conversations(u:User=Depends(me),d:Session=Depends(db)):
    ai_verified(u,d);rows=d.query(AiConversation).filter_by(user_id=u.id).order_by(AiConversation.updated_at.desc()).limit(100).all();return [{"id":c.id,"title":c.title,"updated_at":c.updated_at.isoformat()} for c in rows]

@app.post("/ai/conversations")
def ai_new_conversation(x:AiConversationIn,u:User=Depends(me),d:Session=Depends(db)):
    ai_verified(u,d);c=AiConversation(user_id=u.id,title=(x.title.strip() or "Nouvelle conversation")[:120]);d.add(c);d.commit();d.refresh(c);return {"id":c.id,"title":c.title}

@app.get("/ai/conversations/{cid}")
def ai_conversation(cid:int,u:User=Depends(me),d:Session=Depends(db)):
    ai_verified(u,d);c=d.get(AiConversation,cid)
    if not c or c.user_id!=u.id:raise HTTPException(404)
    return {"id":c.id,"title":c.title,"messages":[{"role":m.role,"content":m.content,"created_at":m.created_at.isoformat()} for m in d.query(AiMessage).filter_by(conversation_id=cid).order_by(AiMessage.id).all()]}

@app.delete("/ai/conversations/{cid}")
def ai_delete_conversation(cid:int,u:User=Depends(me),d:Session=Depends(db)):
    ai_verified(u,d);c=d.get(AiConversation,cid)
    if not c or c.user_id!=u.id:raise HTTPException(404)
    d.query(AiMessage).filter_by(conversation_id=cid).delete();d.delete(c);d.commit();return {"ok":True}

@app.get("/ai/memory")
def ai_memory(u:User=Depends(me),d:Session=Depends(db)):
    ai_verified(u,d);return ai_memories(u.id,d)

@app.put("/ai/memory")
def ai_memory_put(x:AiMemoryIn,u:User=Depends(me),d:Session=Depends(db)):
    ai_verified(u,d);row=d.query(AiMemory).filter_by(user_id=u.id,key=x.key.strip()).first()
    if not row:row=AiMemory(user_id=u.id,key=x.key.strip(),value=x.value.strip());d.add(row)
    else:row.value=x.value.strip();row.updated_at=datetime.utcnow()
    d.commit();return {"ok":True}

@app.post("/ai/chat")
def ai_chat(x:AiChat,u:User=Depends(me),d:Session=Depends(db)):
    ai_verified(u,d);cid=x.conversation_id
    c=d.get(AiConversation,cid) if cid else None
    if not c or c.user_id!=u.id:
        c=AiConversation(user_id=u.id,title=(x.message.strip()[:60] or "Nouvelle conversation"));d.add(c);d.commit();d.refresh(c)
    # Memory commands: "retiens que clé = valeur"
    low=x.message.lower().strip()
    if low.startswith("retiens que "):
        body=x.message[11:].strip();parts=re.split(r"\s*=\s*|\s+est\s+",body,maxsplit=1)
        if len(parts)==2:
            key,value=parts[0].strip()[:80],parts[1].strip()[:4000];row=d.query(AiMemory).filter_by(user_id=u.id,key=key).first()
            if row:row.value=value;row.updated_at=datetime.utcnow()
            else:d.add(AiMemory(user_id=u.id,key=key,value=value))
            answer=f"Je retiens : {key} = {value}."
        else:answer="Utilise par exemple : retiens que projet = DuraTube."
    else:
        recent_rows=d.query(AiMessage).filter_by(conversation_id=c.id).order_by(AiMessage.id.desc()).limit(8).all()
        recent=[{"role":m.role,"content":m.content} for m in reversed(recent_rows)]
        answer=BRAIN.answer(x.message,ai_memories(u.id,d),recent)
    d.add(AiMessage(conversation_id=c.id,role="user",content=x.message));d.add(AiMessage(conversation_id=c.id,role="assistant",content=answer));c.updated_at=datetime.utcnow();d.commit()
    return {"text":answer,"conversation_id":c.id,"engine":"DuraBrain Local 1.2"}
