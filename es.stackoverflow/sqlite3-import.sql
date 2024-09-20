CREATE TABLE Posts (
    Id INT,
    AcceptedAnswerId INT NULL DEFAULT NULL,
    AnswerCount INT DEFAULT 0,
    Body TEXT, DATETIME(6) NULL DEFAULT NULL,
    ClosedDate DATETIME(6) NULL DEFAULT NULL,
    CommentCount INT DEFAULT 0,(6) NULL DEFAULT NULL,
    CommunityOwnedDate DATETIME(6) NULL DEFAULT NULL,
    ContentLicense TEXT NULL DEFAULT NULL,NULL,
    CreationDate DATETIME(6) NULL DEFAULT NULL,
    FavoriteCount INT DEFAULT 0, NULL DEFAULT NULL,
    LastActivityDate DATETIME(6) NULL DEFAULT NULL,
    LastEditDate DATETIME(6) NULL DEFAULT NULL,
    LastEditorDisplayName TEXT,EFAULT NULL,
    LastEditorUserId INT NULL DEFAULT NULL,
    OwnerDisplayName TEXT,EFAULT NULL,
    OwnerUserId INT NULL DEFAULT NULL,
    ParentId INT NULL DEFAULT NULL,, 2 = Answer
    PostTypeId INT, -- 1 = Question, 2 = Answer
    Score INT DEFAULT 0,
    Tags TEXT,,
    Title TEXT,NT DEFAULT 0,
    ViewCount INT DEFAULT 0,
    PRIMARY KEY(Id)
);
.import --csv --skip 1 -v Posts.csv Posts

CREATE TABLE Tags (
    Id INT,
    Count INT DEFAULT 0,
    ExcerptPostId INT NULL DEFAULT NULL,
    TagName TEXT,
    WikiPostId INT NULL DEFAULT NULL,
    PRIMARY KEY(Id)
);
.import --csv --skip 1 -v Tags.csv Tags

CREATE TABLE Users (
    Id INT,
    AboutMe TEXT,
    AccountId INT,
    CreationDate DATETIME(6) NULL DEFAULT NULL,
    DisplayName TEXT,
    DownVotes INT DEFAULT 0,
    LastAccessDate DATETIME(6) NULL DEFAULT NULL,
    Location TEXT,
    Reputation INT DEFAULT 0,
    UpVotes INT DEFAULT 0,
    Views INT DEFAULT 0,
    WebsiteUrl TEXT,
    PRIMARY KEY(Id)
);
.import --csv --skip 1 -v Users.csv Users

CREATE TABLE Comments (
    Id INT,
    ContentLicense TEXT NULL DEFAULT NULL,
    CreationDate DATETIME(6) NULL DEFAULT NULL,
    PostId INT NULL DEFAULT NULL,
    Score INT DEFAULT 0,
    Text TEXT,
    UserDisplayName TEXT,
    UserId INT NULL DEFAULT NULL,
    PRIMARY KEY(Id)
);
.import --csv --skip 1 -v Comments.csv Comments

CREATE TABLE Votes (
    Id INT,
    BountyAmount INT DEFAULT 0,
    CreationDate DATETIME(6) NULL DEFAULT NULL,
    PostId INT NULL DEFAULT NULL,
    UserId INT NULL DEFAULT NULL,
    VoteTypeId INT,
    PRIMARY KEY(Id)
);
.import --csv --skip 1 -v Votes.csv Votes
