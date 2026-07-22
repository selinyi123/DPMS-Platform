-- Persist the distinct collection/favorite phase used by Weibo, Douyin and
-- Xiaohongshu. Without this upgrade a confirmed remote collection can fail
-- during local phase settlement and become an unknown external outcome.
ALTER TABLE task_phases
  MODIFY COLUMN phase
    ENUM('init','followed','liked','commented','favorited','reposted','completed')
    DEFAULT 'init';
