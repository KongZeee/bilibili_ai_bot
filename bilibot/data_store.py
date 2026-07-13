"""
数据存储基类

提供线程安全的JSON读写，用于记忆、配置、日志等所有持久化数据。
"""
import json
import os
import threading
import logging
import weakref
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("bilibot.storage")


class _FileLock:
    """可被弱引用引用的锁包装器。

    threading.Lock 本身不支持 weakref，这里用带 __weakref__ 槽的轻量类包装，
    配合 WeakValueDictionary 实现“无人使用即自动回收”的按文件锁缓存，
    避免 _locks 字典随访问过的文件名无限增长。
    """
    __slots__ = ("_lock", "__weakref__")

    def __init__(self):
        self._lock = threading.Lock()

    def __enter__(self):
        self._lock.acquire()
        return self

    def __exit__(self, *exc):
        self._lock.release()


class DataStore:
    """JSON文件数据存储"""
    
    def __init__(self, data_dir: str = "./data"):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._locks: weakref.WeakValueDictionary = weakref.WeakValueDictionary()
        self._global_lock = threading.Lock()

    def _get_lock(self, filename: str) -> _FileLock:
        """获取指定文件的锁

        使用 WeakValueDictionary 缓存按文件的锁：只要还有线程持有该锁的强引用
        （例如处于 ``with lock:`` 块中），字典里的弱引用就保持有效，新调用会复用
        同一把锁；当所有使用者释放强引用后，锁对象会被自动 GC，字典条目随之消失，
        避免 _locks 随文件名无限增长。
        """
        with self._global_lock:
            lock = self._locks.get(filename)
            if lock is None:
                lock = _FileLock()
                self._locks[filename] = lock
            return lock
    
    def _get_filepath(self, filename: str) -> Path:
        """获取文件完整路径"""
        # 防止路径穿越攻击
        safe_name = os.path.basename(filename)
        return self.data_dir / safe_name
    
    def load_json(self, filename: str, default: Any = None) -> Any:
        """
        加载JSON文件
        
        Args:
            filename: 文件名（不含路径）
            default: 文件不存在时的默认值
            
        Returns:
            解析后的数据，文件不存在则返回default
        """
        filepath = self._get_filepath(filename)
        lock = self._get_lock(filename)
        
        with lock:
            if not filepath.exists():
                return default
            
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    content = f.read()
                    if not content.strip():
                        return default
                    return json.loads(content)
            except (json.JSONDecodeError, IOError) as e:
                logger.error(f"加载 {filename} 失败: {e}")
                return default
    
    def save_json(self, filename: str, data: Any) -> bool:
        """
        保存JSON文件
        
        Args:
            filename: 文件名（不含路径）
            data: 要保存的数据
            
        Returns:
            是否保存成功
        """
        filepath = self._get_filepath(filename)
        lock = self._get_lock(filename)
        
        with lock:
            try:
                # 写入临时文件再重命名，保证原子性
                temp_path = filepath.with_suffix(".tmp")
                with open(temp_path, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2, default=str)
                
                # 原子替换
                temp_path.replace(filepath)
                return True
            except IOError as e:
                logger.error(f"保存 {filename} 失败: {e}")
                return False
    
    def delete_file(self, filename: str) -> bool:
        """删除文件"""
        filepath = self._get_filepath(filename)
        lock = self._get_lock(filename)
        
        with lock:
            try:
                if filepath.exists():
                    filepath.unlink()
                    return True
                return False
            except IOError as e:
                logger.error(f"删除 {filename} 失败: {e}")
                return False
    
    def list_files(self) -> List[str]:
        """列出所有数据文件"""
        return [f.name for f in self.data_dir.glob("*.json")]
    
    def clear_all(self) -> int:
        """清空所有数据文件，返回删除的文件数"""
        count = 0
        for f in self.data_dir.glob("*.json"):
            try:
                f.unlink()
                count += 1
            except IOError:
                pass
        return count
