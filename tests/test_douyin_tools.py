import pytest
from douyin_summary_mcp import server as server_module
from douyin_summary_mcp.server import create_server
from douyin_summary_mcp.errors import MCPError
from test_tool_annotations import _settings
from test_video_analysis import manager


@pytest.mark.asyncio
async def test_four_new_tools_annotations_and_auth(tmp_path,monkeypatch):
    mcp=create_server(_settings(tmp_path))
    monkeypatch.setattr(server_module,'get_access_token',lambda:None)
    for name,args,read_only,open_world in [
        ('prepare_douyin_video',{'share_link':'link'},True,True),
        ('start_douyin_analysis',{'context_token':'token','confirm_free_quota_usage':True},False,True),
        ('get_video_analysis_job',{'job_id':'job'},True,False),
        ('get_video_frames',{'visual_ref':'ref'},True,False),
    ]:
        tool=await mcp.get_tool(name)
        assert tool is not None
        assert tool.annotations.read_only_hint is read_only
        assert tool.annotations.open_world_hint is open_world
        with pytest.raises(PermissionError):
            result=tool.fn(**args)
            if hasattr(result,'__await__'):
                await result


@pytest.mark.asyncio
async def test_frame_tool_returns_standard_mcp_image_blocks(tmp_path,monkeypatch):
    from types import SimpleNamespace
    obj,db,tokens=manager(tmp_path)
    p=await obj.prepare('link',1)
    a=obj.start(p['context_token'],True,1)
    await obj.prepare_source(db.get_job(obj.get_job(a['job_id'],1)['asr_job_id']))
    ref=obj.get_job(a['job_id'],1)['visual_ref']
    monkeypatch.setattr(server_module,'VideoAnalysis',lambda *args,**kwargs:obj)
    monkeypatch.setattr(server_module,'get_access_token',lambda:SimpleNamespace(claims={'sub':'1'}))
    mcp=create_server(_settings(tmp_path))
    tool=await mcp.get_tool('get_video_frames')
    result=tool.fn(visual_ref=ref,limit=4)
    assert [block.type for block in result]==['text','image','text','image','text','image','text','image','text']
    assert all(block.mime_type=='image/jpeg' for block in result if block.type=='image')


@pytest.mark.asyncio
async def test_user_bound_douyin_transcript_cannot_use_old_tool_to_bypass(tmp_path):
    from douyin_summary_mcp.service import ZephyrService
    from test_video_analysis import NOW
    from datetime import timedelta
    obj,db,tokens=manager(tmp_path)
    p=await obj.prepare('link',1)
    a=obj.start(p['context_token'],True,1)
    asr_id=obj.get_job(a['job_id'],1)['asr_job_id']
    db.complete_transcription(job_id=asr_id,actual_seconds=6,original_duration_ms=6000,
        content_duration_ms=6000,segments=[{'text':'hello'}],content_hash='hash',now=NOW,cache_ttl=timedelta(days=30))
    service=ZephyrService(obj.settings,db,object(),tokens)
    ref=obj.get_job(a['job_id'],1)['transcript_ref']
    with pytest.raises(MCPError):
        await service.transcript_page(ref,None,50,github_user_id=2)
    with pytest.raises(MCPError):
        service.get_job(asr_id,github_user_id=2)
    assert (await service.transcript_page(ref,None,50,github_user_id=1))['segments'][0]['text']=='hello'
    old_tool_ref=service.get_job(asr_id,github_user_id=1)['transcript_ref']
    with pytest.raises(MCPError):
        await service.transcript_page(old_tool_ref,None,50,github_user_id=2)
