import os, uuid, jwt, boto3, random, hashlib, ast, math, re, html, secrets, logging, json
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from fastapi import FastAPI, HTTPException, Depends, Header, UploadFile, File, Form
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from fastapi.middleware.gzip import GZipMiddleware
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

app=FastAPI(title="Dura Cloud",version="4.5")
app.add_middleware(GZipMiddleware, minimum_size=700)

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
    return {"service":"Dura Cloud","version":"4.5","status":"online" if DATABASE_READY else "degraded",
            "release":"titan-desktop-candidate","duratube":True,"studio":True,"duramail":True,
            "duraia":"DuraBrain Core 3.5","database":DATABASE_READY,"r2":bool(R2_ENDPOINT),
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
            "warnings":CONFIG_WARNINGS,"version":"4.5"}

@app.get("/ready")
def ready():
    if not DATABASE_READY:
        raise HTTPException(503,"Dura Cloud démarre mais la base de données n'est pas prête. Consulte /health et les logs Render.")
    return {"ready":True,"version":"4.5"}

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
    # V4.5: une adresse DuraMail ne crée plus une chaîne implicitement.
    # La chaîne passe obligatoirement par /v45/channel/apply + validation admin.
    u=User(address=a,display_name=x.display_name.strip(),channel_name="",password_hash=ph.hash(x.password));d.add(u);d.commit();d.refresh(u)
    d.add(ThemeProfile(user_id=u.id));d.commit()
    # Même un ancien client est rattaché à la chaîne officielle dès l'inscription.
    try:v45_ensure_official_subscription(d,u)
    except Exception:logger.exception("Abonnement officiel automatique incomplet à l'inscription")
    return {"token":make_token(u),"user":pub_user(u)}
@app.post("/auth/login")
def login(x:Login,d:Session=Depends(db)):
    u=d.query(User).filter(User.address==clean_address(x.address)).first()
    if not u:raise HTTPException(401,"Adresse ou mot de passe incorrect.")
    try:ok=ph.verify(u.password_hash,x.password)
    except VerifyMismatchError:ok=False
    if not ok:raise HTTPException(401,"Adresse ou mot de passe incorrect.")
    if u.is_banned:raise HTTPException(403,"Compte suspendu.")
    # Répare automatiquement l'abonnement système si un ancien compte l'avait perdu.
    try:v45_ensure_official_subscription(d,u)
    except Exception:logger.exception("Abonnement officiel automatique incomplet à la connexion")
    return {"token":make_token(u),"user":pub_user(u)}
@app.get("/auth/me")
def auth_me(u:User=Depends(me)):return pub_user(u)

@app.get("/bootstrap")
def bootstrap_payload(u:User=Depends(me),d:Session=Depends(db)):
    t=d.query(ThemeProfile).filter_by(user_id=u.id).first() or ThemeProfile(user_id=u.id)
    unread=d.query(Mail).filter(Mail.recipient_id==u.id,Mail.trash_recipient==False,Mail.is_read==False).count()
    channel_data=None
    if u.channel_name:
        p=d.query(ChannelProfile).filter_by(user_id=u.id).first() or ChannelProfile(user_id=u.id)
        channel_data={"name":u.channel_name,"description":p.description or "","subscribers":d.query(Subscription).filter_by(channel_id=u.id).count(),"videos":d.query(Video).filter_by(owner_id=u.id,status="published").count()}
    return {"user":pub_user(u),"unread":unread,"theme":{k:getattr(t,k) for k in ["accent","background","surface","text","font","radius","density","graphic"]},"channel":channel_data,"version":"4.5"}

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
    # Compatibilité d'API conservée, mais plus de contournement de la vérification créateur.
    if u.channel_name:raise HTTPException(409,"Tu as déjà une chaîne.")
    raise HTTPException(403,"La création directe de chaîne est désactivée. Envoie une vidéo de vérification depuis DuraTube.")
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
    rows=x.all();rows.sort(key=lambda v:(bool(v.is_featured),v.id),reverse=True);return [v45_video(v,d) for v in rows]
@app.get("/feed")
def feed(d:Session=Depends(db)):
    rows=d.query(Video).filter(Video.status=="published",Video.is_short==False).all();active={p.video_id:p for p in d.query(Promotion).filter(Promotion.status=="active").all() if p.delivered_impressions<p.target_impressions}
    rows.sort(key=lambda v:((1000000 if v.is_featured else 0)+(500000 if v.id in active else 0)+(v.views or 0)+(v.likes or 0)*5+v.id),reverse=True)
    for v in rows[:30]:
        p=active.get(v.id)
        if p:p.delivered_impressions+=1;p.status="completed" if p.delivered_impressions>=p.target_impressions else "active"
    d.commit();return [v45_video(v,d)|{"promoted":v.id in active} for v in rows[:30]]
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
    return [v45_video(v,d) for v in rows[:12]]

@app.get("/channels")
def channels(q:str="",d:Session=Depends(db)):
    x=d.query(User).filter(User.is_banned==False,User.channel_name!="")
    if q:x=x.filter(User.channel_name.ilike(f"%{q}%"))
    return [pub_user(u)|{"subscribers":d.query(Subscription).filter_by(channel_id=u.id).count(),"videos":d.query(Video).filter_by(owner_id=u.id,status="published").count(),"logo_url":f"/channel/{u.id}/logo" if (d.query(ChannelProfile).filter_by(user_id=u.id).first() and d.query(ChannelProfile).filter_by(user_id=u.id).first().logo_key) else None} for u in x.all()]
@app.post("/subscriptions/{channel_id}")
def subscribe(channel_id:int,u:User=Depends(me),d:Session=Depends(db)):
    if channel_id==u.id:raise HTTPException(400)
    ch=d.get(User,channel_id)
    if not ch:raise HTTPException(404)
    if not d.query(Subscription).filter_by(subscriber_id=u.id,channel_id=channel_id).first():d.add(Subscription(subscriber_id=u.id,channel_id=channel_id))
    pref=d.query(SubscriptionSettingV45).filter_by(subscriber_id=u.id,channel_id=channel_id).first()
    if not pref:d.add(SubscriptionSettingV45(subscriber_id=u.id,channel_id=channel_id,notifications_enabled=True))
    d.commit();return {"subscribed":True,"mandatory":bool(ch.is_official)}
@app.delete("/subscriptions/{channel_id}")
def unsubscribe(channel_id:int,u:User=Depends(me),d:Session=Depends(db)):
    ch=d.get(User,channel_id)
    if ch and ch.is_official:raise HTTPException(403,"La chaîne officielle Dura est un abonnement système obligatoire.")
    s=d.query(Subscription).filter_by(subscriber_id=u.id,channel_id=channel_id).first()
    if s:d.delete(s)
    d.query(SubscriptionSettingV45).filter_by(subscriber_id=u.id,channel_id=channel_id).delete();d.commit()
    return {"subscribed":False}
@app.get("/subscriptions/status/{channel_id}")
def subscription_status(channel_id:int,u:User=Depends(me),d:Session=Depends(db)):
    ch=d.get(User,channel_id)
    if ch and ch.is_official:v45_ensure_official_subscription(d,u)
    return {"subscribed":d.query(Subscription).filter_by(subscriber_id=u.id,channel_id=channel_id).first() is not None,"subscribers":d.query(Subscription).filter_by(channel_id=channel_id).count(),"mandatory":bool(ch and ch.is_official and ch.id!=u.id)}

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

@app.get("/studio/comments")
def studio_comments(u:User=Depends(me),d:Session=Depends(db)):
    if not u.channel_name:raise HTTPException(403,"Aucune chaîne.")
    vids={v.id:v for v in d.query(Video).filter_by(owner_id=u.id).all()}
    if not vids:return []
    rows=d.query(Comment).filter(Comment.video_id.in_(list(vids.keys()))).order_by(Comment.id.desc()).limit(200).all()
    out=[]
    for c in rows:
        author=d.get(User,c.user_id);v=vids.get(c.video_id)
        out.append({"id":c.id,"video_id":c.video_id,"video_title":v.title if v else "","username":(author.channel_name or author.display_name) if author else "Utilisateur","content":c.content,"created_at":c.created_at.strftime("%d/%m/%Y %H:%M") if c.created_at else ""})
    return out

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
    if x.is_banned and d.query(PermanentBanV45).filter_by(user_id=uid).first():
        raise HTTPException(409,"Ce compte porte un bannissement copyright permanent.")
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
    KNOWLEDGE={
        "api":"Une API est une interface qui permet à deux logiciels de communiquer avec des règles précises.",
        "serveur":"Un serveur reçoit des requêtes, exécute une logique et renvoie des données ou des fichiers aux applications clientes.",
        "cloud":"Le cloud désigne des ressources informatiques accessibles à distance, par Internet, au lieu d'être uniquement sur le PC de l'utilisateur.",
        "base de données":"Une base de données stocke et organise des informations pour pouvoir les retrouver, les modifier et les relier efficacement.",
        "postgresql":"PostgreSQL est un système de base de données relationnelle robuste et open source.",
        "python":"Python est un langage de programmation généraliste connu pour sa syntaxe lisible et son vaste écosystème.",
        "ia":"Une intelligence artificielle est un système informatique conçu pour effectuer des tâches qui demandent habituellement de l'analyse, de la prédiction ou de la génération.",
        "llm":"Un LLM est un grand modèle de langage entraîné sur énormément de texte pour prédire et générer du langage.",
        "jwt":"Un JWT est un jeton signé qui transporte des informations d'authentification entre un client et un serveur.",
        "r2":"Cloudflare R2 est un stockage d'objets compatible S3, adapté aux médias et fichiers volumineux.",
    }
    def tokenize(self,text):return re.findall(r"[a-zA-ZÀ-ÿ0-9']+",text.lower())
    def sentences(self,text):return [x.strip() for x in re.split(r"(?<=[.!?])\s+|\n+",text.strip()) if x.strip()]
    def safe_math(self,expr):
        allowed={ast.Add:lambda a,b:a+b,ast.Sub:lambda a,b:a-b,ast.Mult:lambda a,b:a*b,ast.Div:lambda a,b:a/b,ast.FloorDiv:lambda a,b:a//b,ast.Mod:lambda a,b:a%b,ast.Pow:lambda a,b:a**b,ast.USub:lambda a:-a,ast.UAdd:lambda a:+a}
        def ev(n):
            if isinstance(n,ast.Expression):return ev(n.body)
            if isinstance(n,ast.Constant) and isinstance(n.value,(int,float)):return n.value
            if isinstance(n,ast.BinOp) and type(n.op) in allowed:
                a,b=ev(n.left),ev(n.right)
                if isinstance(n.op,ast.Pow) and abs(b)>12:raise ValueError()
                return allowed[type(n.op)](a,b)
            if isinstance(n,ast.UnaryOp) and type(n.op) in allowed:return allowed[type(n.op)](ev(n.operand))
            raise ValueError()
        return ev(ast.parse(expr,mode="eval"))
    def summarize(self,text,limit=4):
        sents=self.sentences(text)
        if not sents:return "Je n'ai pas reçu de texte à résumer."
        if len(sents)<=limit:return " ".join(sents)
        words=[w for w in self.tokenize(text) if w not in STOPWORDS and len(w)>2];freq=Counter(words)
        scored=[]
        for i,s in enumerate(sents):
            toks=[w for w in self.tokenize(s) if w not in STOPWORDS];score=sum(freq[w] for w in toks)/(len(toks)+1);scored.append((score,i,s))
        return " ".join(x[2] for x in sorted(sorted(scored,reverse=True)[:limit],key=lambda x:x[1]))
    def rewrite(self,text):
        t=re.sub(r"\s+"," ",text.strip());t=re.sub(r"\s+([,.!?;:])",r"\1",t)
        if t:t=t[0].upper()+t[1:]
        if t and t[-1] not in ".!?":t+="."
        return t
    def brainstorm(self,topic):
        topic=topic.strip() or "ton projet"
        angles=["simple à comprendre","premium","communautaire","viral","utile au quotidien","automatisé","très personnalisable","collaboratif","mobile-first","créateur-first"]
        return "Voici 10 pistes pour **"+topic+"** :\n"+"\n".join(f"{i+1}. Une version {a} de {topic}." for i,a in enumerate(angles))
    def plan(self,goal):
        g=goal.strip().rstrip("?.!") or "ton objectif"
        return f"Voici un plan clair pour **{g}** :\n\n1. Définir le résultat attendu.\n2. Identifier ce qui est indispensable.\n3. Construire une première version fonctionnelle.\n4. Tester les parcours principaux.\n5. Corriger les blocages et simplifier l'interface.\n6. Ajouter les fonctions avancées.\n7. Vérifier sécurité, erreurs et sauvegardes.\n8. Préparer une vraie version de publication."
    def previous_user(self,context):
        return next((m.get("content","") for m in reversed(context or []) if m.get("role")=="user"),"")
    def known_definition(self,raw):
        low=raw.lower()
        for k,v in self.KNOWLEDGE.items():
            if k in low:return v
        return None
    def answer(self,message,memories,context=None):
        raw=message.strip();low=raw.lower().strip();tokens=set(self.tokenize(raw));context=context or []
        if not raw:return "Écris-moi quelque chose et je m'en occupe."
        if any(x in low for x in ["présente-toi","presente-toi","qui es-tu","tu es qui"]):
            return "Salut, je suis **DuraIA**, l'assistant de l'écosystème Dura. Je fonctionne avec **DuraBrain Core 2.0**, un moteur maison sans clé API externe. Je peux t'aider à calculer, résumer, reformuler, organiser des idées, préparer des plans, écrire des brouillons, expliquer certains sujets et travailler avec les services Dura."
        if low in {"bonjour","salut","hello","hey","wesh","yo"} or low.startswith(("bonjour ","salut ","hello ")):
            return "Salut 👋 Je suis DuraIA. Dis-moi ce que tu veux faire et je vais essayer de te donner une réponse directement utile."
        if any(x in low for x in ["que peux-tu faire","tu peux faire quoi","tes capacités","tes capacites"]):
            return "Je peux notamment :\n\n• faire des calculs ;\n• résumer un texte ;\n• reformuler et corriger ;\n• proposer des idées et des titres ;\n• construire un plan ;\n• expliquer des notions que je connais ;\n• conserver de petites informations dans ma mémoire Dura ;\n• t'aider sur DuraTube, Studio et DuraMail."
        if low.startswith(("calcule ","calcul ")):
            expr=raw.split(" ",1)[1].replace("×","*").replace("÷","/").replace("^","**")
            try:return f"Résultat : **{self.safe_math(expr)}**"
            except Exception:return "Je n'arrive pas à interpréter ce calcul. Essaie par exemple : `calcule (12+8)*3`."
        if re.fullmatch(r"[0-9\s+\-*/().,%^]+",raw):
            try:return f"Résultat : **{self.safe_math(raw.replace('^','**'))}**"
            except Exception:pass
        if low.startswith(("résume ","resume ","résumer ","resumer ")):
            return "**Résumé**\n\n"+self.summarize(raw.split(" ",1)[1] if " " in raw else "")
        if low.startswith(("réécris ","reecris ","corrige ","reformule ","améliore ce texte ","ameliore ce texte ")):
            return self.rewrite(raw.split(" ",1)[1] if " " in raw else "")
        if low.startswith(("idées ","idees ","brainstorm ","donne-moi des idées","donne moi des idees")):
            topic=re.sub(r"^(idées|idees|brainstorm|donne-moi des idées|donne moi des idees)\s*(pour|sur)?\s*","",raw,flags=re.I)
            return self.brainstorm(topic)
        if low.startswith(("plan ","planifie ","organise ","fais-moi un plan","fais moi un plan")):
            goal=re.sub(r"^(plan|planifie|organise|fais-moi un plan|fais moi un plan)\s*(pour|de)?\s*","",raw,flags=re.I)
            return self.plan(goal)
        if low.startswith(("titre ","titres ","donne-moi des titres","donne moi des titres")):
            topic=re.sub(r"^(titre|titres|donne-moi des titres|donne moi des titres)\s*(pour|sur)?\s*","",raw,flags=re.I) or "ton sujet"
            return "Voici quelques titres :\n\n"+"\n".join([f"• {topic} : le guide complet",f"• J'ai testé {topic}",f"• Tout comprendre sur {topic}",f"• {topic}, mais en mieux",f"• Ce que personne ne te dit sur {topic}"])
        if low.startswith(("écris ","ecris ","rédige ","redige ")):
            topic=raw.split(" ",1)[1] if " " in raw else "ton sujet"
            return f"Voici un premier brouillon sur **{topic}** :\n\n{topic.capitalize()} mérite une présentation claire, avec une idée principale dès le début. Ensuite, développe les points les plus importants dans un ordre logique, ajoute un exemple concret, puis termine par une conclusion qui résume l'essentiel.\n\nSi tu me donnes le format exact attendu, je peux le restructurer davantage."
        if any(x in low for x in ["qui suis-je","que sais-tu sur moi","tu sais quoi sur moi"]):
            if not memories:return "Je n'ai encore rien mémorisé à ton sujet dans DuraIA."
            return "Voici ce que j'ai en mémoire :\n"+"\n".join(f"• {k} : {v}" for k,v in memories.items())
        if "duratube" in low:
            return "**DuraTube** est la plateforme vidéo de l'écosystème Dura. Les comptes utilisent @duramail, les médias sont stockés sur R2 et DuraTube Studio sert à gérer les chaînes, le contenu et les statistiques."
        if "duramail" in low:
            return "**DuraMail** est la messagerie centrale de l'écosystème Dura. Les messages et comptes sont synchronisés par Dura Cloud, avec prise en charge des pièces jointes."
        if "dura" in low and "studio" in low:
            return "**DuraTube Studio** est l'espace créateur : contenu, statistiques, commentaires, personnalisation et communauté."
        definition=self.known_definition(raw)
        if definition and any(x in low for x in ["c'est quoi","cest quoi","qu'est-ce que","explique","définis","definis"]):
            return definition+"\n\nSi tu veux, je peux aussi te le découper en étapes ou donner un exemple."
        if low.startswith(("explique ","explique-moi ","explique moi ")):
            subject=re.sub(r"^explique(?:-moi| moi)?\s+","",raw,flags=re.I)
            definition=self.known_definition(subject)
            if definition:return definition+"\n\n**En pratique :** commence par identifier son rôle, puis ce qu'il reçoit, ce qu'il produit et avec quoi il communique."
            return f"Pour comprendre **{subject}**, sépare le sujet en quatre questions :\n\n1. Qu'est-ce que c'est ?\n2. À quoi ça sert ?\n3. Comment ça fonctionne ?\n4. Quel exemple concret permet de le vérifier ?\n\nDonne-moi une définition, un texte ou des données sur le sujet et je pourrai les organiser plus précisément."
        if low.startswith(("comment ","comment faire ")):
            subject=re.sub(r"^comment(?: faire)?\s+","",raw,flags=re.I)
            return self.plan(subject)
        if low.startswith(("pourquoi ",)):
            subject=raw[9:].strip()
            return f"Pour répondre proprement à **pourquoi {subject}**, il faut regarder la cause immédiate, les facteurs qui l'ont rendue possible et les conséquences. Si tu me donnes le contexte exact, je peux ensuite construire l'explication point par point."
        if "merci" in tokens:return "Avec plaisir."
        if any(x in low for x in ["et après","et apres","continue","suite"]):
            prev=self.previous_user(context)
            if prev:return self.plan(prev)
        important=[w for w in self.tokenize(raw) if w not in STOPWORDS and len(w)>2][:8]
        subject=" ".join(important[:5]) or "ta demande"
        return f"Je vois que tu veux travailler sur **{subject}**. Je peux déjà t'aider de trois façons :\n\n1. clarifier exactement le résultat que tu veux ;\n2. transformer l'idée en étapes concrètes ;\n3. produire un premier brouillon ou une structure testable.\n\nPour cette demande précise, donne-moi les éléments de départ les plus importants et je construis la suite à partir d'eux."

BRAIN=DuraBrain()

def ai_verified(u,d):
    a=d.query(AiAccess).filter_by(user_id=u.id).first()
    if not a or not a.verified:raise HTTPException(403,"Vérifie d'abord ton accès DuraIA.")

def ai_memories(user_id,d):return {m.key:m.value for m in d.query(AiMemory).filter_by(user_id=user_id).all()}

@app.get("/ai/status")
def ai_status(u:User=Depends(me),d:Session=Depends(db)):
    a=d.query(AiAccess).filter_by(user_id=u.id).first();return {"verified":bool(a and a.verified),"engine":"DuraBrain Core 2.0","provider_ready":True,"external_api":False}

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
    return {"text":answer,"conversation_id":c.id,"engine":"DuraBrain Core 2.0"}

# ============================================================================
# DURA CLOUD 4.5 TITAN EXTENSIONS
# Backward compatible with the V4.4 clients while the V4.5 desktop apps use
# the /v45 namespace for live stats, creator verification, mandatory official
# subscriptions, admin directory and DuraBrain Core 3.5.
# ============================================================================

class CreatorVerification(Base):
    __tablename__ = "creator_verifications"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    channel_name = Column(String(80), nullable=False)
    description = Column(Text, default="")
    object_key = Column(String(500), nullable=False)
    status = Column(String(30), default="pending")
    review_note = Column(String(500), default="")
    created_at = Column(DateTime, default=datetime.utcnow)
    reviewed_at = Column(DateTime, nullable=True)
    reviewed_by = Column(Integer, nullable=True)

class SubscriptionSettingV45(Base):
    __tablename__ = "subscription_settings_v45"
    __table_args__ = (UniqueConstraint("subscriber_id", "channel_id", name="uq_subscription_setting_v45"),)
    id = Column(Integer, primary_key=True)
    subscriber_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    channel_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    notifications_enabled = Column(Boolean, default=True)

class PermanentBanV45(Base):
    __tablename__ = "permanent_bans_v45"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    reason = Column(String(500), default="")
    created_by = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

class WatchHistoryV45(Base):
    __tablename__ = "watch_history_v45"
    __table_args__ = (UniqueConstraint("user_id", "video_id", name="uq_watch_history_v45"),)
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    video_id = Column(Integer, ForeignKey("videos.id"), nullable=False, index=True)
    watch_count = Column(Integer, default=1)
    last_watched = Column(DateTime, default=datetime.utcnow)

class ChannelBanV45(BaseModel):
    reason: str = Field(default="Copie/copyright de chaîne", max_length=500)

class NotificationV45(BaseModel):
    enabled: bool = True


def v45_official_user(d):
    return d.query(User).filter(User.is_official == True).order_by(User.id).first()


def v45_ensure_official_subscription(d, u):
    official = v45_official_user(d)
    if not official or official.id == u.id:
        return official
    sub = d.query(Subscription).filter_by(subscriber_id=u.id, channel_id=official.id).first()
    if not sub:
        d.add(Subscription(subscriber_id=u.id, channel_id=official.id))
    pref = d.query(SubscriptionSettingV45).filter_by(subscriber_id=u.id, channel_id=official.id).first()
    if not pref:
        d.add(SubscriptionSettingV45(subscriber_id=u.id, channel_id=official.id, notifications_enabled=True))
    else:
        pref.notifications_enabled = True
    d.commit()
    return official


def v45_video(v, d):
    owner = d.get(User, v.owner_id)
    return pub_video(v) | {
        "verified": bool(owner and owner.is_official),
        "owner_official": bool(owner and owner.is_official),
    }


@app.get("/v45/bootstrap")
def v45_bootstrap(u: User = Depends(me), d: Session = Depends(db)):
    official = v45_ensure_official_subscription(d, u)
    pending = d.query(CreatorVerification).filter_by(user_id=u.id, status="pending").order_by(CreatorVerification.id.desc()).first()
    unread = d.query(Mail).filter(Mail.recipient_id == u.id, Mail.trash_recipient == False, Mail.is_read == False).count()
    return {
        "version": "4.5",
        "user": pub_user(u) | {"verified": bool(u.is_official)},
        "unread": unread,
        "official_channel_id": official.id if official else None,
        "official_channel": official.channel_name if official else "",
        "mandatory_official_subscription": bool(official and official.id != u.id),
        "creator_application": {
            "status": pending.status if pending else "none",
            "name": pending.channel_name if pending else "",
        },
    }


@app.get("/v45/feed")
def v45_feed(limit: int = 30, authorization: str = Header(default=""), d: Session = Depends(db)):
    # Au moins 10 recommandations sont demandées quand le catalogue en contient 10.
    # On ne duplique jamais artificiellement une vidéo juste pour remplir la grille.
    limit = max(10, min(limit, 60))
    rows = d.query(Video).filter(Video.status == "published", Video.is_short == False).all()
    viewer_id = None
    if authorization.startswith("Bearer "):
        try:
            viewer_id = int(jwt.decode(authorization[7:], SECRET, algorithms=["HS256"])["sub"])
        except Exception:
            viewer_id = None
    subscribed = set()
    liked = set()
    watched = {}
    if viewer_id:
        subscribed = {x.channel_id for x in d.query(Subscription).filter_by(subscriber_id=viewer_id).all()}
        liked = {x.video_id for x in d.query(VideoLike).filter_by(user_id=viewer_id).all()}
        watched = {x.video_id: (x.watch_count or 0) for x in d.query(WatchHistoryV45).filter_by(user_id=viewer_id).all()}

    def score(v):
        owner = d.get(User, v.owner_id)
        official = bool(owner and owner.is_official)
        # Chaîne officielle en tête, puis abonnements et engagement.
        # Les contenus déjà likés sont légèrement moins poussés pour diversifier.
        return (
            100000000 if official else 0,
            10000000 if v.is_featured else 0,
            1000000 if v.owner_id in subscribed else 0,
            -25000 if v.id in liked else 0,
            -(watched.get(v.id, 0) * 75000),
            (v.views or 0) + (v.likes or 0) * 12,
            v.id,
        )
    rows.sort(key=score, reverse=True)
    return [v45_video(v, d) for v in rows[:limit]]


@app.get("/v45/shorts")
def v45_shorts(limit: int = 40, d: Session = Depends(db)):
    limit = max(1, min(limit, 80))
    rows = d.query(Video).filter(Video.status == "published", Video.is_short == True).all()
    rows.sort(
        key=lambda v: (
            bool(d.get(User, v.owner_id) and d.get(User, v.owner_id).is_official),
            (v.views or 0) + (v.likes or 0) * 10,
            v.id,
        ),
        reverse=True,
    )
    return [v45_video(v, d) for v in rows[:limit]]


@app.post("/v45/videos/{vid}/view")
def v45_record_view(vid: int, authorization: str = Header(default=""), d: Session = Depends(db)):
    v = d.get(Video, vid)
    if not v or v.status != "published":
        raise HTTPException(404, "Vidéo introuvable.")
    v.views = (v.views or 0) + 1
    viewer_id = None
    if authorization.startswith("Bearer "):
        try:
            viewer_id = int(jwt.decode(authorization[7:], SECRET, algorithms=["HS256"])["sub"])
        except Exception:
            viewer_id = None
    if viewer_id:
        row = d.query(WatchHistoryV45).filter_by(user_id=viewer_id, video_id=vid).first()
        if row:
            row.watch_count = (row.watch_count or 0) + 1
            row.last_watched = datetime.utcnow()
        else:
            d.add(WatchHistoryV45(user_id=viewer_id, video_id=vid, watch_count=1, last_watched=datetime.utcnow()))
    d.commit()
    return {"views": v.views, "tracked": bool(viewer_id)}


@app.get("/v45/videos/{vid}/stats")
def v45_video_stats(vid: int, d: Session = Depends(db)):
    v = d.get(Video, vid)
    if not v or v.status != "published":
        raise HTTPException(404, "Vidéo introuvable.")
    return {
        "id": v.id,
        "views": v.views or 0,
        "likes": v.likes or 0,
        "comments": d.query(Comment).filter_by(video_id=vid).count(),
        "subscribers": d.query(Subscription).filter_by(channel_id=v.owner_id).count(),
    }


@app.get("/v45/videos/stats")
def v45_video_stats_batch(ids: str = "", d: Session = Depends(db)):
    wanted = []
    for part in ids.split(","):
        try:
            wanted.append(int(part))
        except Exception:
            pass
    out = {}
    if not wanted:
        return out
    for v in d.query(Video).filter(Video.id.in_(wanted[:100])).all():
        out[str(v.id)] = {"views": v.views or 0, "likes": v.likes or 0}
    return out


@app.get("/v45/subscriptions/{channel_id}")
def v45_subscription_status(channel_id: int, u: User = Depends(me), d: Session = Depends(db)):
    ch = d.get(User, channel_id)
    if not ch:
        raise HTTPException(404, "Chaîne introuvable.")
    if ch.is_official:
        v45_ensure_official_subscription(d, u)
    subscribed = bool(d.query(Subscription).filter_by(subscriber_id=u.id, channel_id=channel_id).first())
    pref = d.query(SubscriptionSettingV45).filter_by(subscriber_id=u.id, channel_id=channel_id).first()
    return {
        "subscribed": subscribed,
        "mandatory": bool(ch.is_official and ch.id != u.id),
        "notifications": True if ch.is_official else bool(pref.notifications_enabled if pref else subscribed),
    }


@app.post("/v45/subscriptions/{channel_id}")
def v45_subscribe(channel_id: int, u: User = Depends(me), d: Session = Depends(db)):
    if channel_id == u.id:
        raise HTTPException(400, "Impossible de s'abonner à soi-même.")
    ch = d.get(User, channel_id)
    if not ch:
        raise HTTPException(404, "Chaîne introuvable.")
    if not d.query(Subscription).filter_by(subscriber_id=u.id, channel_id=channel_id).first():
        d.add(Subscription(subscriber_id=u.id, channel_id=channel_id))
    pref = d.query(SubscriptionSettingV45).filter_by(subscriber_id=u.id, channel_id=channel_id).first()
    if not pref:
        d.add(SubscriptionSettingV45(subscriber_id=u.id, channel_id=channel_id, notifications_enabled=True))
    d.commit()
    return {"subscribed": True, "mandatory": bool(ch.is_official), "notifications": True}


@app.delete("/v45/subscriptions/{channel_id}")
def v45_unsubscribe(channel_id: int, u: User = Depends(me), d: Session = Depends(db)):
    ch = d.get(User, channel_id)
    if ch and ch.is_official:
        raise HTTPException(403, "La chaîne officielle Dura est un abonnement système obligatoire.")
    row = d.query(Subscription).filter_by(subscriber_id=u.id, channel_id=channel_id).first()
    if row:
        d.delete(row)
    d.query(SubscriptionSettingV45).filter_by(subscriber_id=u.id, channel_id=channel_id).delete()
    d.commit()
    return {"subscribed": False}


@app.put("/v45/subscriptions/{channel_id}/notifications")
def v45_notifications(channel_id: int, x: NotificationV45, u: User = Depends(me), d: Session = Depends(db)):
    ch = d.get(User, channel_id)
    if not ch:
        raise HTTPException(404, "Chaîne introuvable.")
    if ch.is_official and not x.enabled:
        raise HTTPException(403, "Les notifications de la chaîne officielle sont obligatoires.")
    if not d.query(Subscription).filter_by(subscriber_id=u.id, channel_id=channel_id).first():
        raise HTTPException(409, "Abonne-toi d'abord à cette chaîne.")
    pref = d.query(SubscriptionSettingV45).filter_by(subscriber_id=u.id, channel_id=channel_id).first()
    if not pref:
        pref = SubscriptionSettingV45(subscriber_id=u.id, channel_id=channel_id)
        d.add(pref)
    pref.notifications_enabled = True if ch.is_official else x.enabled
    d.commit()
    return {"notifications": bool(pref.notifications_enabled), "mandatory": bool(ch.is_official)}


@app.post("/v45/channel/apply")
async def v45_channel_apply(
    name: str = Form(...),
    description: str = Form(""),
    verification_video: UploadFile = File(...),
    u: User = Depends(me),
    d: Session = Depends(db),
):
    if u.channel_name:
        raise HTTPException(409, "Tu as déjà une chaîne.")
    name = name.strip()[:80]
    if len(name) < 2:
        raise HTTPException(400, "Nom de chaîne trop court.")
    if d.query(User).filter(User.channel_name.ilike(name)).first():
        raise HTTPException(409, "Une chaîne utilise déjà ce nom.")
    if d.query(CreatorVerification).filter(CreatorVerification.channel_name.ilike(name), CreatorVerification.status == "pending").first():
        raise HTTPException(409, "Ce nom est déjà en cours de vérification.")
    if d.query(CreatorVerification).filter_by(user_id=u.id, status="pending").first():
        raise HTTPException(409, "Tu as déjà une demande de création en attente.")
    ext = Path(verification_video.filename or "").suffix.lower()
    if ext not in {".mp4", ".mov", ".mkv", ".webm", ".m4v"}:
        raise HTTPException(400, "La vérification doit être une vidéo MP4/MOV/MKV/WEBM/M4V.")
    reject_oversize(verification_video, 300, "Vidéo de vérification")
    key = f"creator-verification/{u.id}/{uuid.uuid4().hex}{ext}"
    try:
        r2().upload_fileobj(
            verification_video.file,
            R2_BUCKET,
            key,
            ExtraArgs={"ContentType": verification_video.content_type or "video/mp4"},
        )
    except Exception as exc:
        logger.exception("Upload vérification créateur impossible")
        raise HTTPException(503, "Impossible d'envoyer la vidéo de vérification.") from exc
    row = CreatorVerification(
        user_id=u.id,
        channel_name=name,
        description=description[:2000],
        object_key=key,
    )
    d.add(row)
    d.commit()
    d.refresh(row)
    return {
        "ok": True,
        "id": row.id,
        "status": "pending",
        "message": "Demande envoyée. DuraIndustry doit valider la vidéo avant l'ouverture de la chaîne.",
    }


@app.get("/v45/channel/application")
def v45_channel_application(u: User = Depends(me), d: Session = Depends(db)):
    row = d.query(CreatorVerification).filter_by(user_id=u.id).order_by(CreatorVerification.id.desc()).first()
    if not row:
        return {"status": "none"}
    return {
        "id": row.id,
        "channel_name": row.channel_name,
        "status": row.status,
        "review_note": row.review_note or "",
        "created_at": row.created_at.isoformat() if row.created_at else "",
    }


@app.get("/v45/admin/creator-verifications")
def v45_admin_creator_verifications(u: User = Depends(me), d: Session = Depends(db)):
    if not u.is_admin:
        raise HTTPException(403)
    out = []
    for row in d.query(CreatorVerification).order_by(CreatorVerification.id.desc()).limit(500).all():
        account = d.get(User, row.user_id)
        out.append({
            "id": row.id,
            "user_id": row.user_id,
            "address": account.address if account else "",
            "display_name": account.display_name if account else "",
            "channel_name": row.channel_name,
            "description": row.description or "",
            "status": row.status,
            "review_note": row.review_note or "",
            "created_at": row.created_at.isoformat() if row.created_at else "",
            "video_url": f"/v45/admin/creator-verifications/{row.id}/video",
        })
    return out


@app.get("/v45/admin/creator-verifications/{rid}/video")
def v45_admin_creator_video(rid: int, u: User = Depends(me), d: Session = Depends(db)):
    if not u.is_admin:
        raise HTTPException(403)
    row = d.get(CreatorVerification, rid)
    if not row:
        raise HTTPException(404)
    return RedirectResponse(signed(row.object_key))


@app.post("/v45/admin/creator-verifications/{rid}/approve")
def v45_admin_creator_approve(rid: int, u: User = Depends(me), d: Session = Depends(db)):
    if not u.is_admin:
        raise HTTPException(403)
    row = d.get(CreatorVerification, rid)
    if not row or row.status != "pending":
        raise HTTPException(404, "Demande introuvable ou déjà traitée.")
    target = d.get(User, row.user_id)
    if not target:
        raise HTTPException(404)
    if d.query(User).filter(User.id != target.id, User.channel_name.ilike(row.channel_name)).first():
        raise HTTPException(409, "Ce nom de chaîne vient d'être pris.")
    target.channel_name = row.channel_name
    row.status = "approved"
    row.reviewed_at = datetime.utcnow()
    row.reviewed_by = u.id
    profile = d.query(ChannelProfile).filter_by(user_id=target.id).first()
    if not profile:
        d.add(ChannelProfile(user_id=target.id, description=row.description or ""))
    d.commit()
    return {"ok": True, "user": pub_user(target)}


@app.post("/v45/admin/creator-verifications/{rid}/reject")
def v45_admin_creator_reject(
    rid: int,
    note: str = Form("La vidéo ne permet pas de confirmer le créateur."),
    u: User = Depends(me),
    d: Session = Depends(db),
):
    if not u.is_admin:
        raise HTTPException(403)
    row = d.get(CreatorVerification, rid)
    if not row:
        raise HTTPException(404)
    row.status = "rejected"
    row.review_note = note[:500]
    row.reviewed_at = datetime.utcnow()
    row.reviewed_by = u.id
    d.commit()
    return {"ok": True}


@app.post("/v45/admin/channels/{uid}/copyright-ban")
def v45_admin_copyright_ban(uid: int, x: ChannelBanV45, u: User = Depends(me), d: Session = Depends(db)):
    if not u.is_admin:
        raise HTTPException(403)
    target = d.get(User, uid)
    if not target or target.is_official:
        raise HTTPException(400, "Ce compte ne peut pas être banni par cette action.")
    # Le bannissement copyright permanent n'est possible qu'après un signalement
    # explicite de l'administrateur lui-même sur cette chaîne.
    report = d.query(Report).filter_by(reporter_id=u.id, target_type="channel", target_id=uid, status="open").order_by(Report.id.desc()).first()
    if not report:
        raise HTTPException(409, "Signale d'abord cette chaîne depuis DuraTube avec ton compte admin avant le bannissement copyright permanent.")
    target.is_banned = True
    if not d.query(PermanentBanV45).filter_by(user_id=uid).first():
        d.add(PermanentBanV45(user_id=uid, reason=x.reason[:500], created_by=u.id))
    for v in d.query(Video).filter_by(owner_id=uid).all():
        v.status = "private"
    report.status = "resolved"
    d.commit()
    return {"ok": True, "permanent": True, "address": target.address}


@app.get("/v45/admin/duramail-directory")
def v45_admin_duramail_directory(q: str = "", u: User = Depends(me), d: Session = Depends(db)):
    if not u.is_admin:
        raise HTTPException(403)
    query = d.query(User)
    if q:
        query = query.filter(or_(
            User.address.ilike(f"%{q}%"),
            User.display_name.ilike(f"%{q}%"),
            User.channel_name.ilike(f"%{q}%"),
        ))
    return [
        pub_user(x) | {
            "verified": bool(x.is_official),
            "banned": bool(x.is_banned),
            "created_at": x.created_at.isoformat() if x.created_at else "",
        }
        for x in query.order_by(User.address).limit(2500).all()
    ]


class DuraBrainCore3:
    """DuraBrain Core 3.5.

    No AI API key. The engine combines deterministic tools, conversation memory,
    Dura account data, lightweight retrieval and optional public encyclopedia
    lookup. This is deliberately honest: it is not a magically trained frontier
    LLM, but it is much more useful than the old canned fallback.
    """

    def __init__(self):
        self.stop = {"le","la","les","un","une","des","de","du","et","ou","à","a","au","aux","en","dans","sur","pour","par","avec","sans","que","qui","quoi","est","sont","je","tu","il","elle","on","nous","vous","ils","elles","ce","ces","ça","ca","mon","ton","son"}
        self.facts = {
            "france": "La France est un pays d'Europe occidentale. Sa capitale est Paris.",
            "paris": "Paris est la capitale de la France.",
            "python": "Python est un langage de programmation généraliste très utilisé pour le web, l'automatisation, la data et l'IA.",
            "fastapi": "FastAPI est un framework Python moderne pour construire des API web typées.",
            "postgresql": "PostgreSQL est un système de gestion de base de données relationnelle open source.",
            "cloudflare r2": "Cloudflare R2 est un stockage objet compatible S3.",
            "duratube": "DuraTube est la plateforme vidéo de l'écosystème Dura.",
            "duramail": "DuraMail est la messagerie interne @duramail reliée à Dura Cloud.",
            "duratube studio": "DuraTube Studio est l'application créateur et modération de DuraTube.",
            "duraia": "DuraIA est l'assistant de l'écosystème Dura, propulsé par DuraBrain.",
        }

    def tokens(self, text):
        return re.findall(r"[a-zà-ÿ0-9_+-]+", (text or "").lower())

    def safe_calc(self, expr):
        expr = (expr or "").replace("×", "*").replace("÷", "/").replace(",", ".").replace("^", "**")
        expr = re.sub(r"(\d+(?:\.\d+)?)\s*%", r"(\1/100)", expr)
        try:
            node = ast.parse(expr, mode="eval")
            allowed = (ast.Expression, ast.BinOp, ast.UnaryOp, ast.Constant, ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow, ast.USub, ast.UAdd, ast.Call, ast.Name, ast.Load)
            if any(not isinstance(n, allowed) for n in ast.walk(node)):
                return None
            names = {"sqrt": math.sqrt, "sin": math.sin, "cos": math.cos, "tan": math.tan, "pi": math.pi, "e": math.e, "abs": abs, "round": round}
            for n in ast.walk(node):
                if isinstance(n, ast.Name) and n.id not in names:
                    return None
                if isinstance(n, ast.Call) and (not isinstance(n.func, ast.Name) or n.func.id not in names):
                    return None
            value = eval(compile(node, "<durabrain-calc>", "eval"), {"__builtins__": {}}, names)
            if isinstance(value, float):
                value = round(value, 10)
            return str(value)
        except Exception:
            return None

    def summarize(self, text, max_sentences=4):
        text = re.sub(r"\s+", " ", (text or "").strip())
        if not text:
            return "Envoie le texte à résumer."
        sentences = re.split(r"(?<=[.!?])\s+", text)
        if len(sentences) <= max_sentences:
            return text
        words = [w for w in self.tokens(text) if w not in self.stop and len(w) > 2]
        freq = Counter(words)
        ranked = []
        for i, sentence in enumerate(sentences):
            ranked.append((sum(freq[w] for w in self.tokens(sentence)), i, sentence))
        best = sorted(ranked, reverse=True)[:max_sentences]
        return " ".join(x[2] for x in sorted(best, key=lambda x: x[1]))

    def wikipedia(self, query):
        if os.getenv("DURAIA_WEB_KNOWLEDGE", "1") != "1":
            return None
        try:
            import urllib.parse
            import urllib.request
            params = urllib.parse.urlencode({
                "action": "query",
                "generator": "search",
                "gsrsearch": query,
                "gsrlimit": 1,
                "prop": "extracts|info",
                "exintro": 1,
                "explaintext": 1,
                "inprop": "url",
                "format": "json",
                "utf8": 1,
            })
            req = urllib.request.Request(
                "https://fr.wikipedia.org/w/api.php?" + params,
                headers={"User-Agent": "DuraIA/4.5 knowledge-retrieval"},
            )
            with urllib.request.urlopen(req, timeout=4) as response:
                data = json.loads(response.read().decode("utf-8"))
            pages = (data.get("query") or {}).get("pages") or {}
            if not pages:
                return None
            page = next(iter(pages.values()))
            extract = re.sub(r"\s+", " ", page.get("extract", "")).strip()
            if not extract:
                return None
            return self.summarize(extract, 4) + f"\n\nSource de connaissance : Wikipédia — {page.get('fullurl', '')}"
        except Exception:
            return None

    def duckduckgo(self, query):
        if os.getenv("DURAIA_WEB_KNOWLEDGE", "1") != "1":
            return None
        try:
            import urllib.parse
            import urllib.request
            url = "https://api.duckduckgo.com/?" + urllib.parse.urlencode({
                "q": query, "format": "json", "no_html": 1, "skip_disambig": 1, "no_redirect": 1
            })
            req = urllib.request.Request(url, headers={"User-Agent": "DuraIA/4.5 knowledge-retrieval"})
            with urllib.request.urlopen(req, timeout=4) as response:
                data = json.loads(response.read().decode("utf-8"))
            text = re.sub(r"\s+", " ", (data.get("AbstractText") or data.get("Answer") or "")).strip()
            if text:
                source = data.get("AbstractSource") or "DuckDuckGo"
                source_url = data.get("AbstractURL") or ""
                suffix = f"\n\nSource de connaissance : {source}" + (f" — {source_url}" if source_url else "")
                return self.summarize(text, 5) + suffix
        except Exception:
            return None
        return None

    def knowledge(self, query):
        # Réponses factuelles sans clé d'API IA : moteur local + sources publiques.
        local = self.local_fact(query)
        if local:
            return local
        return self.wikipedia(query) or self.duckduckgo(query)

    def contextual_query(self, raw, recent):
        low = raw.lower().strip()
        vague = len(self.tokens(raw)) <= 6 and any(x in low for x in ["et ", "il ", "elle ", "ça", "ca", "celui", "celle", "quand", "où", "ou ", "pourquoi", "comment"])
        if not vague:
            return raw
        previous = [m.get("content", "") for m in (recent or []) if m.get("role") == "user" and m.get("content")]
        if not previous:
            return raw
        return previous[-1] + " ; question suivante : " + raw

    def local_fact(self, text):
        low = text.lower()
        best = None
        score = 0
        for key, value in self.facts.items():
            current = 4 if key in low else 0
            current += sum(1 for t in self.tokens(key) if t in self.tokens(low))
            if current > score:
                best = value
                score = current
        return best if score else None

    def plan(self, goal):
        goal = goal.strip() or "ton objectif"
        return (
            f"Plan pour **{goal}** :\n\n"
            "1. Définir le résultat attendu et les contraintes.\n"
            "2. Préparer les ressources nécessaires.\n"
            "3. Construire une première version testable.\n"
            "4. Tester les cas principaux et les erreurs.\n"
            "5. Corriger les blocages.\n"
            "6. Optimiser l'expérience utilisateur et les performances.\n"
            "7. Valider sécurité, sauvegardes et déploiement."
        )

    def answer(self, message, memories, recent, ecosystem):
        raw = (message or "").strip()
        low = raw.lower()
        if not raw:
            return "Écris-moi une question ou une tâche."
        if re.fullmatch(r"(?:bonjour|salut|hello|hey|coucou)[ !?.]*", low):
            return "Salut. Je suis **DuraIA**, propulsée par **DuraBrain Core 3.5**. Je peux répondre à des questions, calculer, résumer, reformuler, préparer des plans, exploiter tes données Dura et chercher des connaissances sans clé d'API IA."
        if any(x in low for x in ["présente-toi", "presente-toi", "qui es-tu", "tu es qui"]):
            return "Je suis **DuraIA**, l'assistant de l'écosystème Dura. DuraBrain Core 3.5 combine mémoire, outils, données Dura, calcul et récupération de connaissances. Je ne prétends pas être un grand modèle généraliste du niveau de ChatGPT, mais je ne réponds plus avec des plans absurdes à une simple salutation."

        expr = low
        for prefix in ("calcule ", "combien font ", "combien fait ", "résous ", "resous "):
            if expr.startswith(prefix):
                expr = expr[len(prefix):]
        calc = self.safe_calc(expr)
        if calc is not None:
            return f"**Résultat : {calc}**"

        if low.startswith(("résume ", "resume ", "fais un résumé ", "fais un resume ")):
            body = re.sub(r"^(résume|resume|fais un résumé|fais un resume)\s*:?\s*", "", raw, flags=re.I)
            return self.summarize(body)
        if low.startswith(("reformule ", "réécris ", "reecris ", "corrige ")):
            body = raw.split(" ", 1)[1] if " " in raw else ""
            body = re.sub(r"\s+", " ", body).strip()
            if not body:
                return "Envoie le texte à reformuler."
            return body[0].upper() + body[1:] + ("" if body[-1] in ".!?" else ".")
        if low.startswith(("plan ", "planifie ", "organise ", "comment faire ")):
            body = re.sub(r"^(plan|planifie|organise|comment faire)\s*", "", raw, flags=re.I)
            return self.plan(body)
        if low.startswith(("idées ", "idees ", "brainstorm ")):
            topic = raw.split(" ", 1)[1] if " " in raw else "ton projet"
            return "Idées pour **" + topic + "** :\n\n" + "\n".join([
                "• simplifier le premier parcours utilisateur",
                "• ajouter une personnalisation visible",
                "• automatiser les tâches répétitives",
                "• afficher des statistiques vraiment utiles",
                "• prévoir un mode créateur et un mode spectateur",
                "• ajouter une modération claire",
                "• concevoir le fonctionnement hors-ligne/cache",
                "• préparer les tests avant publication",
            ])

        if any(x in low for x in ["mes mails", "mails non lus", "mail non lu", "combien de mails"]):
            return f"Tu as **{ecosystem.get('unread_mail', 0)} mail(s) non lu(s)** dans DuraMail."
        if any(x in low for x in ["mes abonnés", "mes abonnes", "combien d'abonnés", "combien d'abonnes"]):
            if not ecosystem.get("has_channel"):
                return "Ton compte n'a pas encore de chaîne DuraTube validée."
            return f"Ta chaîne **{ecosystem.get('channel', '')}** a **{ecosystem.get('subscribers', 0)} abonné(s)**."
        if any(x in low for x in ["mes vues", "stats de ma chaîne", "statistiques de ma chaîne"]):
            if not ecosystem.get("has_channel"):
                return "Ton compte n'a pas encore de chaîne DuraTube validée."
            return f"Ta chaîne totalise **{ecosystem.get('views', 0)} vues**, **{ecosystem.get('likes', 0)} J'aime** et **{ecosystem.get('videos', 0)} contenu(s)**."
        if any(x in low for x in ["qui suis-je", "que sais-tu sur moi", "tu sais quoi sur moi"]):
            data = [f"Compte : {ecosystem.get('address', '')}"]
            if ecosystem.get("has_channel"):
                data.append(f"Chaîne : {ecosystem.get('channel', '')}")
            data.extend(f"{k} : {v}" for k, v in memories.items())
            return "Voici ce que je peux utiliser :\n" + "\n".join(f"• {x}" for x in data)

        local = self.local_fact(raw)
        if local and any(x in low for x in ["c'est quoi", "cest quoi", "qu'est-ce", "qui est", "quelle est", "quel est", "explique", "définis", "definis"]):
            return local

        if low.startswith(("écris ", "ecris ", "rédige ", "redige ")):
            topic = raw.split(" ", 1)[1] if " " in raw else "ton sujet"
            return f"Voici un brouillon sur **{topic}** :\n\n{topic.capitalize()} doit être présenté avec une idée principale claire dès le début. Développe ensuite les informations essentielles dans un ordre logique, ajoute un exemple concret et termine par une conclusion courte."

        if any(x in low for x in ["quelle heure", "quel jour", "quelle date", "date d'aujourd'hui", "date aujourd'hui"]):
            now = datetime.now(timezone.utc)
            return f"Côté Dura Cloud, nous sommes le **{now.strftime('%d/%m/%Y')}** à **{now.strftime('%H:%M')} UTC**."

        if "?" in raw or low.startswith(("qui ", "quoi ", "où ", "ou ", "quand ", "quel ", "quelle ", "combien ", "pourquoi ", "explique ", "donne-moi ", "donne moi ")):
            query = self.contextual_query(raw, recent)
            found = self.knowledge(query)
            if found:
                return found

        # Dernière tentative de connaissance pour une demande déclarative courte.
        if len(self.tokens(raw)) >= 2:
            found = self.knowledge(self.contextual_query(raw, recent))
            if found:
                return found

        return "Je n'ai pas trouvé de connaissance suffisamment fiable pour répondre sans inventer. Reformule avec un sujet précis, ou demande-moi un calcul, un résumé, une reformulation, un plan, tes statistiques Dura ou une recherche factuelle."


DURABRAIN3 = DuraBrainCore3()


@app.get("/v45/ai/status")
def v45_ai_status(u: User = Depends(me), d: Session = Depends(db)):
    access = d.query(AiAccess).filter_by(user_id=u.id).first()
    return {
        "verified": bool(access and access.verified),
        "engine": "DuraBrain Core 3.5",
        "external_ai_api": False,
        "web_knowledge": os.getenv("DURAIA_WEB_KNOWLEDGE", "1") == "1",
    }


@app.post("/v45/ai/chat")
def v45_ai_chat(x: AiChat, u: User = Depends(me), d: Session = Depends(db)):
    ai_verified(u, d)
    c = d.get(AiConversation, x.conversation_id) if x.conversation_id else None
    if not c or c.user_id != u.id:
        c = AiConversation(user_id=u.id, title=(x.message.strip()[:60] or "Nouvelle conversation"))
        d.add(c)
        d.commit()
        d.refresh(c)

    low = x.message.lower().strip()
    if low.startswith("retiens que "):
        body = x.message[11:].strip()
        parts = re.split(r"\s*=\s*|\s+est\s+", body, maxsplit=1)
        if len(parts) == 2:
            key, value = parts[0].strip()[:80], parts[1].strip()[:4000]
            row = d.query(AiMemory).filter_by(user_id=u.id, key=key).first()
            if row:
                row.value = value
                row.updated_at = datetime.utcnow()
            else:
                d.add(AiMemory(user_id=u.id, key=key, value=value))
            answer = f"Je retiens : {key} = {value}."
        else:
            answer = "Utilise par exemple : retiens que projet = DuraTube."
    else:
        recent_rows = d.query(AiMessage).filter_by(conversation_id=c.id).order_by(AiMessage.id.desc()).limit(12).all()
        recent = [{"role": m.role, "content": m.content} for m in reversed(recent_rows)]
        memories = {m.key: m.value for m in d.query(AiMemory).filter_by(user_id=u.id).all()}
        vids = d.query(Video).filter_by(owner_id=u.id).all()
        ecosystem = {
            "address": u.address,
            "has_channel": bool(u.channel_name),
            "channel": u.channel_name or "",
            "unread_mail": d.query(Mail).filter(Mail.recipient_id == u.id, Mail.trash_recipient == False, Mail.is_read == False).count(),
            "subscribers": d.query(Subscription).filter_by(channel_id=u.id).count() if u.channel_name else 0,
            "views": sum(v.views or 0 for v in vids),
            "likes": sum(v.likes or 0 for v in vids),
            "videos": len(vids),
        }
        answer = DURABRAIN3.answer(x.message, memories, recent, ecosystem)

    d.add(AiMessage(conversation_id=c.id, role="user", content=x.message))
    d.add(AiMessage(conversation_id=c.id, role="assistant", content=answer))
    c.updated_at = datetime.utcnow()
    d.commit()
    return {"text": answer, "conversation_id": c.id, "engine": "DuraBrain Core 3.5"}
