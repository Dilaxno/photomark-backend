"""
Vault Trash and Version models for PostgreSQL
Provides soft-delete (trash) and point-in-time recovery for vaults
"""
from sqlalchemy import Column, Integer, BigInteger, String, Text, DateTime, JSON, Index
from sqlalchemy.sql import func
from core.database import Base


class VaultTrash(Base):
    """
    Soft-deleted vaults stored in trash for recovery.
    Auto-expires after 30 days unless restored.
    """
    __tablename__ = "vault_trash"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    owner_uid = Column(String(128), nullable=False, index=True)
    vault_name = Column(String(255), nullable=False)
    display_name = Column(String(255), nullable=True)
    original_keys = Column(JSON, nullable=False, default=list)
    metadata = Column(JSON, nullable=False, default=dict)
    photo_count = Column(Integer, nullable=False, default=0)
    total_size_bytes = Column(BigInteger, nullable=False, default=0)
    deleted_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    restored_at = Column(DateTime(timezone=True), nullable=True)
    
    __table_args__ = (
        Index('ix_vault_trash_owner_vault', 'owner_uid', 'vault_name'),
        Index('ix_vault_trash_expires', 'expires_at'),
    )
    
    def to_dict(self):
        return {
            "id": self.id,
            "vaultName": self.vault_name,
            "displayName": self.display_name or self.vault_name.replace("_", " "),
            "photoCount": self.photo_count,
            "totalSize": self.total_size_bytes,
            "deletedAt": self.deleted_at.isoformat() if self.deleted_at else None,
            "expiresAt": self.expires_at.isoformat() if self.expires_at else None,
            "restoredAt": self.restored_at.isoformat() if self.restored_at else None,
            "daysRemaining": max(0, (self.expires_at - func.now()).days) if self.expires_at else 0,
        }


class VaultVersion(Base):
    """
    Point-in-time snapshots of vault state for recovery.
    Stores the list of photo keys and metadata at each version.
    """
    __tablename__ = "vault_versions"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    owner_uid = Column(String(128), nullable=False, index=True)
    vault_name = Column(String(255), nullable=False)
    version_number = Column(Integer, nullable=False, default=1)
    snapshot_keys = Column(JSON, nullable=False, default=list)
    metadata = Column(JSON, nullable=False, default=dict)
    photo_count = Column(Integer, nullable=False, default=0)
    total_size_bytes = Column(BigInteger, nullable=False, default=0)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    description = Column(Text, nullable=True)
    
    __table_args__ = (
        Index('ix_vault_versions_owner_vault', 'owner_uid', 'vault_name'),
        Index('ix_vault_versions_created', 'created_at'),
    )
    
    def to_dict(self):
        return {
            "id": self.id,
            "vaultName": self.vault_name,
            "versionNumber": self.version_number,
            "photoCount": self.photo_count,
            "totalSize": self.total_size_bytes,
            "createdAt": self.created_at.isoformat() if self.created_at else None,
            "description": self.description,
        }
